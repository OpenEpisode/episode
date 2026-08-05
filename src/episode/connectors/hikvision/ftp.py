from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer

from episode.connectors.base import Connector
from episode.domain.models import IngestionReceipt, ReceiptStatus
from episode.engine.bus import EventBus, Message
from episode.storage.files import describe_artifact, move_snapshot

if TYPE_CHECKING:
    from episode.config import EpisodeConfig
    from episode.storage.repository import Repository

logger = logging.getLogger(__name__)

_FILENAME_PATTERNS = [
    re.compile(
        r"(?P<ts>\d{14})(?P<ms>\d{3})?_(?P<video_intercom_event>[^_]+)_(?P<ip>(?:\d{1,3}\.){3}\d{1,3})\.jpg$"
    ),
    re.compile(
        r"(?P<ip>[0-9.]+)_(?P<channel>[A-Za-z0-9]+)_(?P<ts>\d{14})(?P<ms>\d{3})?_(?P<event>.+)\.jpg"
    ),
    re.compile(r"_(?P<ts>\d{14})(?P<ms>\d{3})?_(?P<event>.+)\.jpg"),
    re.compile(
        r"--(?P<ipv6>[0-9a-f:-]+)_(?P<channel>[A-Za-z0-9]+)_(?P<ts>\d{14})(?P<ms>\d{3})?_(?P<event>.+)\.jpg",
    ),
]


class FTPConnector(Connector):
    def __init__(
        self,
        name: str,
        bus: EventBus,
        config: dict,
        app_config: EpisodeConfig,
        repo: Repository | None = None,
    ):
        super().__init__(name, bus, config)
        self._app_config = app_config
        self._repo = repo
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

    def status(self) -> dict:
        return {
            **super().status(),
            "host": self._host,
            "port": self._port,
            "upload_dir": self._upload_dir,
            "passive_ports": list(self._passive_port_range),
            "masquerade_address": self._masquerade_address,
        }

    async def start(self):
        self._running = True
        self._loop = asyncio.get_running_loop()
        os.makedirs(self._upload_dir, exist_ok=True)

        authorizer = DummyAuthorizer()
        authorizer.add_user(self._username, self._password, self._upload_dir, perm="elradfmw")

        class EpisodeFTPHandler(FTPHandler):
            parent = self

            def on_file_received(self, filepath):
                self.parent._on_file_received(filepath)

        handler = EpisodeFTPHandler
        handler.authorizer = authorizer
        handler.passive_ports = range(self._passive_port_range[0], self._passive_port_range[1] + 1)
        handler.masquerade_address = self._masquerade_address

        self._server = FTPServer((self._host, self._port), handler)
        logger.info("%s: FTP listening on %s:%s", self.name, self._host, self._port)

        self._loop.run_in_executor(None, self._server.serve_forever)

    async def stop(self):
        self._running = False
        if self._server:
            self._server.close_all()

    def _on_file_received(self, filepath: str):
        self._loop.call_soon_threadsafe(asyncio.ensure_future, self._ingest_snapshot(filepath))

    async def _ingest_snapshot(self, filepath: str):
        try:
            filename = os.path.basename(filepath)
            metadata = self._parse_filename(filename)
            ts = metadata.pop("timestamp", datetime.now(tz=timezone.utc))
            stored_path = move_snapshot(self._app_config.orphans_dir, filepath)
            artifact = describe_artifact(
                stored_path,
                "snapshot",
                "image/jpeg",
                original_filename=filename,
                metadata={"vendor": "hikvision", "transport": "ftp"},
            )

            device_id = ""
            area_id = ""
            status = ReceiptStatus.UNMATCHED
            device = None
            ip = metadata.get("ip_address", "")
            if ip and self._repo:
                device = await self._repo.find_device_by_ip(ip)
                if device:
                    device_id = device.id
                    area_id = device.area_id
                    status = ReceiptStatus.ACCEPTED
                else:
                    logger.warning("No device found for IP %s; preserving %s", ip, filename)
            else:
                logger.warning("Cannot resolve device for %s; preserving as unmatched", filename)

            receipt = IngestionReceipt(
                source="hikvision:ftp",
                observed_at=ts,
                status=status,
                artifact_id=artifact.id,
                device_id=device_id,
                area_id=area_id,
                metadata={"ip_address": ip} if ip else {},
            )
            delivery = {
                "artifact": asdict(artifact),
                "receipt": asdict(receipt),
            }
            if status == ReceiptStatus.UNMATCHED:
                await self._bus.publish(Message(type="receipt.received", data=delivery))
                return

            if device and "doorbell" in device.capabilities:
                metadata["evidence_role"] = "event_attachment"
                metadata["timelapse_eligible"] = False

            metadata["origin"] = "ftp"
            evidence_data = {
                "device_id": device_id,
                "area_id": area_id,
                "timestamp": ts,
                "evidence_type": "snapshot",
                "file_path": stored_path,
                "mime_type": "image/jpeg",
                "original_filename": filename,
                "artifact_id": artifact.id,
                "byte_size": artifact.byte_size,
                "sha256": artifact.sha256,
                "metadata": metadata,
            }
            await self._bus.publish(
                Message(
                    type="evidence.received",
                    data={**delivery, "evidence": evidence_data},
                )
            )
            logger.debug("%s: ingested snapshot %s", self.name, filename)
        except Exception:
            logger.exception("%s: failed to ingest %s", self.name, filepath)

    @staticmethod
    def _parse_filename(filename: str) -> dict:
        for pattern in _FILENAME_PATTERNS:
            m = pattern.match(filename)
            if m:
                groups = m.groupdict()
                result = {}
                if groups.get("ip"):
                    result["ip_address"] = groups["ip"]
                if groups.get("ipv6"):
                    result["ip_address"] = groups["ipv6"]
                ts_str = groups.get("ts", "")
                ms_str = groups.get("ms", "")
                if ts_str:
                    try:
                        base = datetime.strptime(ts_str, "%Y%m%d%H%M%S")
                        ms = int(ms_str) if ms_str else 0
                        result["timestamp"] = base.replace(
                            tzinfo=timezone(timedelta(hours=1)), microsecond=ms * 1000
                        )
                    except ValueError:
                        pass
                event_type = groups.get("event") or groups.get("video_intercom_event")
                if event_type:
                    result["event_type"] = event_type.lower()
                if groups.get("video_intercom_event"):
                    result["filename_profile"] = "video_intercom"
                return result
        return {}
