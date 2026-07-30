from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import uvicorn
from fastapi.staticfiles import StaticFiles

from episode import __version__
from episode.actions.snapshot import SnapshotEngine
from episode.api.routes import create_api
from episode.config import EpisodeConfig, load_config
from episode.connectors.hikvision.alarm_server import AlarmServerConnector
from episode.connectors.hikvision.ftp import FTPConnector
from episode.connectors.hikvision.isapi import ISAPIConnector
from episode.connectors.onvif import ONVIFConnector
from episode.domain.models import Area, Device
from episode.engine.bus import EventBus
from episode.engine.engine import EpisodeEngine
from episode.media import MediaRegistry
from episode.recording.engine import RecordingEngine
from episode.storage.repository import Repository

logger = logging.getLogger(__name__)


class Application:
    def __init__(self, config: EpisodeConfig):
        self._config = config
        self._bus = EventBus()
        self._repo = Repository(config)
        self._media = MediaRegistry()
        self._engine = EpisodeEngine(self._repo, self._bus, config.episode_timeout)
        self._recorder = RecordingEngine(
            self._repo,
            self._bus,
            config.data_dir,
            segment_seconds=config.actions.recording.segment_seconds,
            media=self._media,
        )
        self._snapshotter = SnapshotEngine(self._bus, self._media, config)
        self._connectors = []
        self._fastapi_app = create_api(self._repo, config.data_dir, config.snapshot_window)

    async def start(self):
        logging.basicConfig(
            level=getattr(logging, self._config.log_level.upper(), logging.INFO),
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("aiosqlite").setLevel(logging.WARNING)
        logging.getLogger("pyftpdlib").setLevel(logging.WARNING)

        logger.info("Initializing storage...")
        await self._repo.initialize()

        logger.info("Loading configured areas and devices...")
        configured_area_ids = set()
        for area_data in self._config.areas:
            await self._repo.upsert_area(Area(**area_data))
            configured_area_ids.add(area_data["id"])
        for existing in await self._repo.list_areas():
            if existing.id not in configured_area_ids:
                await self._repo.delete_area(existing.id)
                logger.info("Removed stale area %s (%s)", existing.id, existing.name)

        configured_device_ids = set()
        for device_data in self._config.devices:
            await self._repo.upsert_device(Device(**device_data))
            configured_device_ids.add(device_data["id"])
        for existing in await self._repo.list_devices():
            if existing.id not in configured_device_ids:
                await self._repo.delete_device(existing.id)
                logger.info("Removed stale device %s (%s)", existing.id, existing.name)

        logger.info("Starting Episode Engine...")
        await self._engine.start()

        logger.info("Starting Recording Engine...")
        await self._recorder.start()

        if self._config.actions.snapshot.enabled:
            logger.info("Starting Snapshot Engine...")
            await self._snapshotter.start()
        else:
            logger.info("Snapshot action disabled by policy")

        logger.info("Starting connectors...")

        # System-level connectors come from the config directly
        for conn_cfg in self._config.connectors:
            if not conn_cfg.enabled:
                continue
            conn = self._build_connector(conn_cfg)
            if conn:
                if isinstance(conn, AlarmServerConnector):
                    conn.mount(self._fastapi_app)
                self._connectors.append(conn)
                await conn.start()

        # Per-device connectors are auto-created from device capabilities
        devices = await self._repo.list_devices()
        for device in devices:
            if "onvif" in device.capabilities and device.ip_address:
                conn = self._build_onvif_connector(device)
                self._connectors.append(conn)
                await conn.start()

            cap = device.get_config("isapi")
            if (
                "isapi" in device.capabilities
                and cap
                and cap.build_url(device.ip_address, device.username, device.password)
            ):
                conn = self._build_isapi_connector(device)
                self._connectors.append(conn)
                await conn.start()

        # Register status endpoint before static mount so routes take precedence
        self._register_status_endpoint()

        # Mount static UI last so connector routes take precedence
        ui_dir = Path(__file__).resolve().parent / "ui"
        if ui_dir.is_dir():
            self._fastapi_app.mount("/", StaticFiles(directory=str(ui_dir), html=True), name="ui")

        logger.info(
            "Starting API on %s:%s...",
            self._config.api_host,
            self._config.api_port,
        )
        uv_level = self._config.log_level.lower() if os.environ.get("DEBUG") == "1" else "warning"
        cfg = uvicorn.Config(
            app=self._fastapi_app,
            host=self._config.api_host,
            port=self._config.api_port,
            log_level=uv_level,
        )
        self._server = uvicorn.Server(cfg)
        await self._server.serve()

    async def shutdown(self):
        logger.info("Shutting down...")
        for conn in self._connectors:
            await conn.stop()
        await self._snapshotter.stop()
        await self._recorder.stop()
        await self._engine.stop()
        await self._repo.close()

    def _register_status_endpoint(self):
        @self._fastapi_app.get("/api/v1/status")
        async def system_status():
            connectors = [c.status() for c in self._connectors]
            return {
                "server": {"running": True, "version": __version__},
                "engine": self._engine.status(),
                "recorder": self._recorder.status(),
                "snapshotter": {
                    **self._snapshotter.status(),
                    "enabled": self._config.actions.snapshot.enabled,
                },
                "connectors": connectors,
            }

    def _build_connector(self, cfg):
        t = cfg.type
        if t == "alarm_server":
            return AlarmServerConnector(
                cfg.settings.get("name", t),
                self._bus,
                cfg.settings,
                self._config,
                repo=self._repo,
            )
        if t == "ftp":
            return FTPConnector(
                cfg.settings.get("name", t), self._bus, cfg.settings, self._config, repo=self._repo
            )
        logger.warning("Unknown connector type: %s", t)
        return None

    def _build_onvif_connector(self, device: Device) -> ONVIFConnector:
        cap = device.get_config("onvif")
        settings = {
            "protocol": cap.protocol if cap and cap.protocol else "http",
            "port": cap.port if cap and cap.port else 80,
            "path": cap.path if cap and cap.path else "/onvif/device_service",
            **(cap.settings if cap else {}),
        }
        return ONVIFConnector(
            f"ONVIF:{device.name}",
            self._bus,
            settings,
            self._config,
            device,
            self._repo,
            self._media,
        )

    def _build_isapi_connector(self, device: Device) -> ISAPIConnector:
        cap = device.get_config("isapi")
        isapi_url = (
            cap.build_url(device.ip_address, device.username, device.password) if cap else ""
        )
        if not isapi_url and device.ip_address:
            isapi_url = f"http://{device.ip_address}/ISAPI/Event/notification/alertStream"
        ignore_events: list[str] = (cap.settings or {}).get("ignore_events", []) if cap else []
        settings = {
            "name": f"ISAPI:{device.name}",
            "url": isapi_url,
            "username": device.username,
            "password": device.password,
            "device_id": device.id,
            "area_id": device.area_id,
            "ignore_events": ignore_events,
        }
        return ISAPIConnector(settings["name"], self._bus, settings, self._config)


def create_app(config: EpisodeConfig | None = None) -> Application:
    if config is None:
        config = load_config()
    return Application(config)


async def run_application() -> None:
    app = Application(load_config())
    try:
        await app.start()
    finally:
        await app.shutdown()


def main():
    try:
        asyncio.run(run_application())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
