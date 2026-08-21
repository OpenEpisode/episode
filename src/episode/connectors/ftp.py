from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer

from episode.ingestion.models import FileIngressDelivery

if TYPE_CHECKING:
    from episode.config import EpisodeConfig
    from episode.ingestion.service import IngestionService

logger = logging.getLogger(__name__)


class FTPConnector:
    """Generic FTP transport; file interpretation belongs to ingress plugins."""

    def __init__(
        self,
        name: str,
        ingestion: IngestionService,
        config: dict,
        app_config: EpisodeConfig,
    ):
        self.name = name
        self._running = False
        self._ingestion = ingestion
        self._host = config.get("host", "0.0.0.0")
        self._port = config.get("port", 2121)
        self._username = config.get("username", "episode")
        self._password = config.get("password", "episode")
        passive_ports = config.get("passive_ports", [30000, 30009])
        if (
            not isinstance(passive_ports, list)
            or len(passive_ports) != 2
            or int(passive_ports[0]) > int(passive_ports[1])
        ):
            raise ValueError("FTP passive_ports must be a [first, last] port range")
        self._passive_port_range = (int(passive_ports[0]), int(passive_ports[1]))
        self._masquerade_address = config.get("masquerade_address")
        self._upload_dir = config.get(
            "upload_dir",
            os.path.join(app_config.data_dir, "ftp_incoming"),
        )
        self._server: FTPServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server_future: asyncio.Future | None = None
        self._ingest_tasks: set[asyncio.Task] = set()
        self._uploads_received = 0
        self._uploads_failed = 0
        self._last_upload_at: datetime | None = None
        self._last_error: str | None = None

    def status(self) -> dict:
        return {
            "name": self.name,
            "type": "ftp",
            "running": self._running,
            "host": self._host,
            "port": self._port,
            "upload_dir": self._upload_dir,
            "passive_ports": list(self._passive_port_range),
            "masquerade_address": self._masquerade_address,
            "uploads_received": self._uploads_received,
            "uploads_failed": self._uploads_failed,
            "last_upload_at": self._last_upload_at,
            "last_error": self._last_error,
        }

    async def start(self):
        if self._running:
            return
        self._running = True
        self._loop = asyncio.get_running_loop()
        os.makedirs(self._upload_dir, exist_ok=True)

        authorizer = DummyAuthorizer()
        authorizer.add_user(self._username, self._password, self._upload_dir, perm="elradfmw")

        class EpisodeFTPHandler(FTPHandler):
            parent = self

            def on_file_received(self, filepath):
                self.parent._on_file_received(filepath, self.remote_ip)

        handler = EpisodeFTPHandler
        handler.authorizer = authorizer
        handler.passive_ports = range(self._passive_port_range[0], self._passive_port_range[1] + 1)
        handler.masquerade_address = self._masquerade_address

        self._server = FTPServer((self._host, self._port), handler)
        logger.info("%s: FTP listening on %s:%s", self.name, self._host, self._port)
        # Give the blocking I/O loop a finite poll interval so close_all() can
        # wake it reliably during container shutdown. Without this, an idle
        # server may leave the executor thread blocked until Docker kills it.
        serve = partial(self._server.serve_forever, timeout=0.5, handle_exit=False)
        self._server_future = self._loop.run_in_executor(None, serve)

    async def stop(self):
        self._running = False
        if self._server:
            self._server.close_all()
            self._server = None
        if self._server_future:
            try:
                await asyncio.wait_for(self._server_future, timeout=5)
            except TimeoutError:
                self._server_future.cancel()
            self._server_future = None
        if self._ingest_tasks:
            await asyncio.gather(*self._ingest_tasks, return_exceptions=True)

    def _on_file_received(self, filepath: str, remote_ip: str = "") -> None:
        if self._loop and self._running:
            self._loop.call_soon_threadsafe(self._schedule_ingest, filepath, remote_ip)

    def _schedule_ingest(self, filepath: str, remote_ip: str = "") -> None:
        if not self._running:
            return
        task = asyncio.create_task(
            self._ingest_file(filepath, remote_ip),
            name=f"ftp-ingest:{os.path.basename(filepath)}",
        )
        self._ingest_tasks.add(task)
        task.add_done_callback(self._ingest_tasks.discard)

    async def _ingest_file(self, filepath: str, remote_ip: str = "") -> None:
        filename = os.path.basename(filepath)
        media_type = mimetypes.guess_type(filename, strict=False)[0] or "application/octet-stream"
        received_at = datetime.now(tz=timezone.utc)
        try:
            await self._ingestion.accept_file(
                FileIngressDelivery(
                    source="ftp:upload",
                    transport="ftp",
                    received_at=received_at,
                    file_path=Path(filepath),
                    media_type=media_type,
                    original_filename=filename,
                    metadata={
                        "connector_type": "ftp",
                        "connector_name": self.name,
                        **({"remote_ip": remote_ip} if remote_ip else {}),
                    },
                )
            )
            self._uploads_received += 1
            self._last_upload_at = received_at
            self._last_error = None
            logger.debug("%s: preserved upload %s", self.name, filename)
        except Exception as exc:
            self._uploads_failed += 1
            self._last_error = str(exc)
            logger.exception("%s: failed to preserve upload %s", self.name, filepath)
