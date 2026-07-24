from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import asdict
from typing import TYPE_CHECKING
from urllib.parse import urlparse, urlunparse

import httpx

from episode.connectors.base import Connector
from episode.connectors.hikvision.parser import ingest_hikvision_xml
from episode.domain.models import ReceiptStatus
from episode.engine.bus import EventBus, Message

if TYPE_CHECKING:
    from episode.config import EpisodeConfig

logger = logging.getLogger(__name__)

_BOUNDARY_RE = re.compile(rb"^-+\d+")


class ISAPIConnector(Connector):
    def __init__(self, name: str, bus: EventBus, config: dict, app_config: EpisodeConfig):
        super().__init__(name, bus, config)
        self._app_config = app_config
        self._url = config.get(
            "url",
            "http://localhost/ISAPI/Event/notification/alertStream",
        )
        self._auth = httpx.DigestAuth(
            config.get("username", "admin"),
            config.get("password", ""),
        )
        self._device_id = config.get("device_id", "")
        self._area_id = config.get("area_id", "")
        self._ignore_events: list[str] = config.get("ignore_events", [])
        self._client: httpx.AsyncClient | None = None
        self._last_event_time: str | None = None
        self._stream_active = False

    def status(self) -> dict:
        return {
            **super().status(),
            "url": self._safe_url(),
            "device_id": self._device_id,
            "area_id": self._area_id,
            "stream_active": self._stream_active,
            "last_event": self._last_event_time,
        }

    async def start(self):
        self._running = True
        self._client = httpx.AsyncClient(auth=self._auth, timeout=None)
        logger.info(
            "%s: monitoring %s via ISAPI (ignore_events=%s)",
            self.name,
            self._safe_url(),
            self._ignore_events,
        )
        asyncio.create_task(self._stream())

    async def stop(self):
        self._running = False
        if self._client:
            await self._client.aclose()

    def _safe_url(self) -> str:
        parsed = urlparse(self._url)
        netloc = parsed.hostname or parsed.netloc
        if parsed.port and parsed.port not in (80, 443):
            netloc = f"{netloc}:{parsed.port}"
        return urlunparse(parsed._replace(netloc=netloc))

    async def _stream(self):
        while self._running:
            try:
                async with self._client.stream("GET", self._url) as response:
                    response.raise_for_status()
                    self._stream_active = True
                    buffer = b""
                    async for chunk in response.aiter_bytes():
                        buffer += chunk
                        parts = buffer.split(b"\r\n\r\n")
                        if len(parts) > 1:
                            for part in parts[:-1]:
                                await self._parse_part(part)
                            buffer = parts[-1]
            except httpx.RequestError as e:
                self._stream_active = False
                logger.warning("%s: connection error: %s", self.name, e)
                await asyncio.sleep(5)
            except Exception:
                self._stream_active = False
                logger.exception("%s: stream error", self.name)
                await asyncio.sleep(5)

    async def _parse_part(self, data: bytes):
        if _BOUNDARY_RE.match(data):
            return
        xml_start = data.find(b"<?xml")
        if xml_start == -1:
            return
        delivery = ingest_hikvision_xml(
            data[xml_start:],
            self._device_id,
            self._area_id,
            "hikvision:isapi",
            self._app_config.orphans_dir,
        )
        if delivery.event is None:
            await self._bus.publish(
                Message(
                    type="receipt.received",
                    data={
                        "artifact": asdict(delivery.artifact),
                        "receipt": asdict(delivery.receipt),
                    },
                )
            )
            return

        event_type = delivery.event.get("event_type", "")
        if event_type in self._ignore_events:
            delivery.receipt.status = ReceiptStatus.IGNORED
            await self._bus.publish(
                Message(
                    type="receipt.received",
                    data={
                        "artifact": asdict(delivery.artifact),
                        "receipt": asdict(delivery.receipt),
                    },
                )
            )
            logger.debug("Ignoring event type '%s' for device %s", event_type, self._device_id)
            return

        await self._bus.publish(
            Message(
                type="event.received",
                data={
                    "event": delivery.event,
                    "artifact": asdict(delivery.artifact),
                    "receipt": asdict(delivery.receipt),
                },
            )
        )
        self._last_event_time = delivery.event.get("timestamp")
