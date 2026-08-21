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
from episode.api.runtime import OperationalView
from episode.config import EpisodeConfig, load_config
from episode.connectors.base import ManagedConnector
from episode.connectors.event_api import EventAPIConnector
from episode.connectors.ftp import FTPConnector
from episode.connectors.http_ingress import HTTPIngressConnector
from episode.engine.bus import EventBus
from episode.engine.engine import EpisodeEngine
from episode.ingestion.router import IngressRouter
from episode.ingestion.service import IngestionService
from episode.inventory import DeviceValidationService, InventoryService
from episode.lifecycle import Lifecycle
from episode.media import MediaRegistry
from episode.media.previews import CurrentViewService
from episode.media.timelapse import TimelapseService
from episode.plugins import PluginContext, PluginManager, builtin_plugin_registry
from episode.plugins.api import register_plugins_api
from episode.plugins.deliveries import RawPluginDeliveryStore
from episode.plugins.external import discover_external_plugins
from episode.recording.engine import RecordingEngine
from episode.storage.repository import Repository

logger = logging.getLogger(__name__)


class Application:
    def __init__(self, config: EpisodeConfig):
        self._config = config
        self._plugin_reload_lock = asyncio.Lock()
        self._lifecycle = Lifecycle()
        self._bus = EventBus()
        self._repo = Repository(config)
        self._engine = EpisodeEngine(self._repo, self._bus, config.episode_timeout)
        self._ingress_router = IngressRouter()
        self._ingestion = IngestionService(
            config.data_dir,
            self._repo,
            self._engine,
            self._ingress_router,
        )
        self._raw_plugin_deliveries = RawPluginDeliveryStore(self._ingestion)
        self._media = MediaRegistry()
        self._timelapses = TimelapseService(self._repo, config.data_dir)
        self._recorder = RecordingEngine(
            self._repo,
            self._bus,
            config.data_dir,
            segment_seconds=config.actions.recording.segment_seconds,
            media=self._media,
        )
        self._snapshotter = SnapshotEngine(self._bus, self._media, config.data_dir)
        self._current_views = CurrentViewService(self._media, self._recorder)
        self._configured_connector_types = {
            connector.type for connector in config.connectors if connector.enabled
        }
        self._plugin_registry = builtin_plugin_registry()
        for registration in discover_external_plugins(
            Path(config.plugins_dir),
            config.plugins,
        ):
            try:
                self._plugin_registry.register(registration)
            except ValueError as error:
                logger.warning("External plugin %s was not registered: %s", registration.id, error)
        self._plugins = PluginManager((), self._plugin_context(()))
        self._inventory = InventoryService(
            self._repo,
            on_device_configuration_changed=self.reload_configured_plugins,
        )
        self._connectors: list[ManagedConnector] = []
        self._operations = OperationalView(
            version=__version__,
            engine_status=self._engine.status,
            recorder_status=self._recorder.status,
            snapshot_status=self._snapshotter.status,
            connector_statuses=lambda: [connector.status() for connector in self._connectors],
            plugin_statuses=self._plugins.statuses,
            snapshots_enabled=config.actions.snapshot.enabled,
        )
        self._validation = DeviceValidationService(
            runtime_integrations=lambda device: self._operations.device_detail(device)[
                "integrations"
            ],
            integration_validators=self._plugin_registry.validators(),
            integration_registrations=self._plugin_registry.device_integrations(),
        )
        self._fastapi_app = create_api(
            self._repo,
            config.data_dir,
            config.snapshot_window,
            self._timelapses,
            operations=self._operations,
            inventory=self._inventory,
            validator=self._validation,
            current_views=self._current_views,
        )
        register_plugins_api(self._fastapi_app, self._plugins)

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
        await self._lifecycle.start(
            "Storage",
            self._repo.initialize,
            self._repo.close,
        )

        logger.info("Loading persistent Area and Device inventory...")
        configured_devices = await self._inventory.configured_devices()
        configured_device_types = {
            config_type
            for device in configured_devices
            for config_type in device.get("configs", {})
        }
        self._plugins.configure(
            self._plugin_registry.for_configuration(
                configured_device_types,
                self._configured_connector_types,
            ),
            self._plugin_context(configured_devices),
        )

        logger.info("Starting Episode Engine...")
        await self._lifecycle.start(
            "Episode Engine",
            self._engine.start,
            self._engine.stop,
        )

        logger.info("Starting media services...")
        await self._lifecycle.start(
            "Timelapse service",
            self._timelapses.start,
            self._timelapses.stop,
        )

        logger.info("Starting Recording Engine...")
        await self._lifecycle.start(
            "Recording Engine",
            self._recorder.start,
            self._recorder.stop,
        )

        if self._config.actions.snapshot.enabled:
            logger.info("Starting Snapshot Engine...")
            await self._lifecycle.start(
                "Snapshot Engine",
                self._snapshotter.start,
                self._snapshotter.stop,
            )
        else:
            logger.info("Snapshot action disabled by policy")

        logger.info("Starting configured plugins...")
        await self._lifecycle.start(
            "Plugin manager",
            self._plugins.start,
            self._plugins.stop,
        )

        logger.info("Starting connectors...")

        # System-level connectors come from the config directly
        for conn_cfg in self._config.connectors:
            if not conn_cfg.enabled:
                continue
            conn = self._build_connector(conn_cfg)
            if conn:
                if isinstance(conn, (EventAPIConnector, HTTPIngressConnector)):
                    conn.mount(self._fastapi_app)
                self._connectors.append(conn)
                await self._lifecycle.start(
                    f"Connector {conn.name}",
                    conn.start,
                    conn.stop,
                )

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
        await self._lifecycle.shutdown()

    async def reload_configured_plugins(self) -> None:
        async with self._plugin_reload_lock:
            configured_devices = await self._inventory.configured_devices()
            configured_device_types = {
                config_type
                for device in configured_devices
                for config_type in device.get("configs", {})
            }
            logger.info("Reloading plugins from saved Device configuration...")
            await self._plugins.stop()
            self._plugins.configure(
                self._plugin_registry.for_configuration(
                    configured_device_types,
                    self._configured_connector_types,
                ),
                self._plugin_context(configured_devices),
            )
            await self._plugins.start()
            logger.info("Configured plugins reloaded")

    def _plugin_context(self, configured_devices) -> PluginContext:
        return PluginContext(
            plugins_dir=Path(self._config.plugins_dir),
            configured_devices=tuple(configured_devices),
            raw_delivery_sink=self._raw_plugin_deliveries,
            ingress_router=self._ingress_router,
            media_registry=self._media,
            device_update_sink=self._repo.upsert_device,
        )

    def _build_connector(self, cfg):
        t = cfg.type
        if t == "alarm_server":
            return HTTPIngressConnector(
                cfg.settings.get("name", t),
                self._ingestion,
                cfg.settings,
                self._config.api_port,
                connector_type=t,
            )
        if t == "event_api":
            return EventAPIConnector(
                cfg.settings.get("name", "Event API"),
                self._ingestion,
                self._ingress_router,
                cfg.settings,
                self._config.api_port,
            )
        if t == "ftp":
            return FTPConnector(
                cfg.settings.get("name", t), self._ingestion, cfg.settings, self._config
            )
        logger.warning("Unknown connector type: %s", t)
        return None


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
