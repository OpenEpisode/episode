from __future__ import annotations

import logging
from dataclasses import asdict
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request

from episode.connectors.hikvision.parser import HikvisionEvent, ingest_hikvision_xml
from episode.engine.bus import EventBus, Message

if TYPE_CHECKING:
    from episode.config import EpisodeConfig
    from episode.storage.repository import Repository

logger = logging.getLogger(__name__)

_XML_START = b"<?xml"


class AlarmServerConnector:
    def __init__(
        self,
        name: str,
        bus: EventBus,
        config: dict,
        app_config: EpisodeConfig,
        repo: Repository | None = None,
    ):
        self.name = name
        self._bus = bus
        self._config = config
        self._app_config = app_config
        self._repo = repo
        self._device_id = config.get("device_id", "")
        self._area_id = config.get("area_id", "")
        self._path = config.get("path", "/alarm")
        self._running = False
        self._request_count = 0

    def status(self) -> dict:
        return {
            "name": self.name,
            "type": "alarm_server",
            "running": self._running,
            "path": self._path,
            "port": self._app_config.api_port,
            "requests_handled": self._request_count,
        }

    def mount(self, app: FastAPI):
        path = self._path

        @app.post(path)
        async def handle_alarm(request: Request):
            self._request_count += 1
            body = await request.body()
            xml_start = body.find(_XML_START)
            if xml_start == -1:
                logger.warning("No XML found in alarm payload (%d bytes)", len(body))
                return {"status": "ok"}
            xml_data = body[xml_start:]
            boundary_end = xml_data.find(b"\r\n--")
            if boundary_end != -1:
                xml_data = xml_data[:boundary_end]

            # Resolve device from camera IP in the XML payload
            device_id = self._device_id
            area_id = self._area_id
            if self._repo:
                parsed = HikvisionEvent.from_bytes(xml_data)
                if parsed is not None and parsed.ip_address:
                    device = await self._repo.find_device_by_ip(parsed.ip_address)
                    if device:
                        device_id = device.id
                        area_id = device.area_id
                        logger.debug(
                            "Resolved alarm from %s to device %s (area %s)",
                            parsed.ip_address,
                            device_id,
                            area_id,
                        )

            delivery = ingest_hikvision_xml(
                xml_data,
                device_id,
                area_id,
                "hikvision:alarm_server",
                self._app_config.orphans_dir,
            )
            message_type = "event.received" if delivery.event is not None else "receipt.received"
            data = {
                "artifact": asdict(delivery.artifact),
                "receipt": asdict(delivery.receipt),
            }
            if delivery.event is not None:
                data["event"] = delivery.event
            await self._bus.publish(Message(type=message_type, data=data))
            return {"status": "ok"}

        logger.info("%s: mounted at %s", self.name, path)

    async def start(self):
        self._running = True

    async def stop(self):
        self._running = False
