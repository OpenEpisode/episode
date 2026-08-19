from __future__ import annotations

import asyncio
import json
import shutil
import socket
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import pytest

from episode import plugin_api
from episode.config import EpisodeConfig, ExternalPluginConfig
from episode.domain.models import Area, Device, ReceiptStatus
from episode.engine.bus import EventBus
from episode.engine.engine import EpisodeEngine
from episode.ingestion.models import IngressDelivery
from episode.ingestion.router import IngressRouter
from episode.ingestion.service import IngestionService
from episode.media import MediaRegistry
from episode.plugins.deliveries import RawPluginDeliveryStore
from episode.plugins.external import discover_external_plugins
from episode.plugins.external.runtime import _ExternalMedia
from episode.plugins.manager import PluginManager
from episode.plugins.models import PluginContext, PluginState
from episode.storage.repository import Repository

EXAMPLE_PLUGIN = Path(__file__).parents[1] / "examples" / "plugins" / "udp-sensor"


def _install_example(tmp_path: Path) -> Path:
    plugins_dir = tmp_path / "plugins"
    shutil.copytree(EXAMPLE_PLUGIN, plugins_dir / "udp-sensor")
    return plugins_dir


def test_unconfigured_external_plugins_are_not_discovered_or_imported(tmp_path, monkeypatch):
    plugins_dir = _install_example(tmp_path)

    def unexpected_parse(_root):
        raise AssertionError("unconfigured manifests should not be inspected")

    monkeypatch.setattr("episode.plugins.external.discovery.parse_manifest", unexpected_parse)

    assert discover_external_plugins(plugins_dir, []) == []
    assert not any(name.startswith("_episode_external_example_udp_sensor") for name in sys.modules)


def test_missing_and_incompatible_plugins_report_safe_states(tmp_path):
    plugins_dir = _install_example(tmp_path)
    manifest_path = plugins_dir / "udp-sensor" / "episode-plugin.json"
    manifest_path.write_text(
        manifest_path.read_text().replace('"plugin_api": "1"', '"plugin_api": "99"')
    )
    configured = ExternalPluginConfig(id="example-udp-sensor", device_ids=["sensor"])

    incompatible = discover_external_plugins(plugins_dir, [configured])[0]
    missing = discover_external_plugins(
        plugins_dir,
        [ExternalPluginConfig(id="not-installed")],
    )[0]

    assert incompatible.unavailable_state == PluginState.INCOMPATIBLE
    assert "requires API 99" in incompatible.unavailable_error
    assert missing.unavailable_state == PluginState.NOT_INSTALLED
    assert "episode-plugin.json" in missing.unavailable_error
    assert not any(name.startswith("_episode_external_example_udp_sensor") for name in sys.modules)


def test_external_plugin_directory_cannot_escape_plugins_root(tmp_path):
    outside = tmp_path / "outside"
    shutil.copytree(EXAMPLE_PLUGIN, outside)
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / "example-udp-sensor").symlink_to(outside, target_is_directory=True)

    registration = discover_external_plugins(
        plugins_dir,
        [ExternalPluginConfig(id="example-udp-sensor", device_ids=["sensor"])],
    )[0]

    assert registration.unavailable_state == PluginState.INCOMPLETE
    assert "escapes the configured plugin root" in registration.unavailable_error
    assert not any(name.startswith("_episode_external_example_udp_sensor") for name in sys.modules)


def test_external_plugin_configuration_rejects_duplicate_ids():
    with pytest.raises(ValueError, match="duplicate ids"):
        EpisodeConfig(
            plugins=[
                {"id": "example"},
                {"id": "example"},
            ]
        )


def test_external_media_registration_is_scoped_and_removed_on_close(tmp_path):
    registry = MediaRegistry()
    assigned = plugin_api.DeviceConfig(
        id="assigned-camera",
        name="Assigned camera",
        device_type="camera",
        area_id="garden",
    )
    media = _ExternalMedia(
        "camera-plugin",
        PluginContext(tmp_path, media_registry=registry),
        (assigned,),
    )
    source = plugin_api.MediaSource(
        device_id=assigned.id,
        stream_uri="rtsp://192.0.2.20/live",
    )

    media.register(source)
    assert registry.get(assigned.id).stream_uri == source.stream_uri
    with pytest.raises(ValueError, match="not assigned"):
        media.register(
            plugin_api.MediaSource(
                device_id="unassigned-camera",
                stream_uri="rtsp://192.0.2.21/live",
            )
        )

    media.close()
    assert registry.get(assigned.id) is None


@pytest.mark.asyncio
async def test_external_plugin_preserves_udp_delivery_then_creates_episode(tmp_path):
    plugins_dir = _install_example(tmp_path)
    config = EpisodeConfig(data_dir=str(tmp_path / "data"), episode_timeout=30)
    repository = Repository(config)
    await repository.initialize()
    await repository.upsert_area(Area(id="garden", name="Garden"))
    sensor = Device(
        id="garden-tripwire",
        name="Garden tripwire",
        device_type="sensor",
        area_id="garden",
    )
    unrelated = Device(
        id="private-camera",
        name="Private camera",
        device_type="camera",
        area_id="garden",
        username="private-user",
        password="private-password",
    )
    await repository.upsert_device(sensor)
    await repository.upsert_device(unrelated)
    engine = EpisodeEngine(repository, EventBus(), timeout=30)
    await engine.start()
    router = IngressRouter()
    ingestion = IngestionService(config.data_dir, repository, engine, router)
    configured = ExternalPluginConfig(
        id="example-udp-sensor",
        device_ids=[sensor.id],
        settings={"host": "127.0.0.1", "port": 0},
    )
    registrations = discover_external_plugins(plugins_dir, [configured])
    context = PluginContext(
        plugins_dir=plugins_dir,
        configured_devices=(asdict(sensor), asdict(unrelated)),
        raw_delivery_sink=RawPluginDeliveryStore(ingestion),
        ingress_router=router,
    )
    manager = PluginManager(registrations, context)

    try:
        assert not any(
            name.startswith("_episode_external_example_udp_sensor") for name in sys.modules
        )
        await manager.start()
        status = manager.statuses()[0]
        assert status["state"] == PluginState.READY
        assert status["version"] == "0.1.0"
        assert status["integration"]["capabilities"] == ["events"]
        assert any(name.startswith("_episode_external_example_udp_sensor") for name in sys.modules)
        host, port = status["metrics"]["listen_address"]

        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sender.sendto(b"tripwire:active", (host, port))
        finally:
            sender.close()

        events = []
        receipts = []
        for _attempt in range(100):
            episodes = await repository.list_episodes()
            if episodes:
                events = await repository.list_events(episode_id=episodes[0].id)
                receipts = await repository.list_ingestion_receipts(episode_id=episodes[0].id)
                if events and receipts:
                    break
            await asyncio.sleep(0.01)
        assert len(episodes) == 1
        assert len(events) == 1
        assert events[0].device_id == sensor.id
        assert events[0].area_id == "garden"
        assert events[0].event_type == "tripwire"
        assert events[0].source == "example:udp"
        assert len(receipts) == 1
        assert receipts[0].status == ReceiptStatus.ACCEPTED
        artifact = await repository.get_raw_artifact(receipts[0].artifact_id)
        assert artifact is not None
        assert artifact.sealed
        assert Path(artifact.file_path).read_bytes() == b"tripwire:active"
    finally:
        await manager.stop()
        await engine.stop()
        await repository.close()


@pytest.mark.asyncio
async def test_external_ingress_plugin_can_interpret_shared_preserved_delivery(tmp_path):
    plugin_root = tmp_path / "plugins" / "shared-example"
    plugin_root.mkdir(parents=True)
    (plugin_root / "episode-plugin.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "shared-example",
                "name": "Shared example",
                "version": "1.0.0",
                "plugin_api": "1",
                "kind": "ingress",
                "entrypoint": "plugin.py:create_plugin",
                "capabilities": ["events"],
            }
        )
    )
    (plugin_root / "plugin.py").write_text(
        """from episode.plugin_api import (EventObservation, HandlerRegistration,
HandlerResult, PluginState, PluginStatus)

class Plugin:
    def __init__(self, context):
        assert context.devices == ()
        self.context = context
    async def start(self):
        self.context.ingress.register(HandlerRegistration(
            id="shared", matcher=lambda item: item.transport == "http",
            handler=self.handle))
    async def handle(self, delivery):
        return HandlerResult(claimed=True, event=EventObservation(
            timestamp=delivery.received_at, event_type="contact",
            device_id=delivery.payload.decode(), source="example:shared"))
    async def stop(self):
        self.context.ingress.unregister("shared")
    def status(self):
        return PluginStatus(state=PluginState.READY)

def create_plugin(context):
    return Plugin(context)
"""
    )
    config = EpisodeConfig(data_dir=str(tmp_path / "data"), episode_timeout=30)
    repository = Repository(config)
    await repository.initialize()
    await repository.upsert_area(Area(id="hall", name="Hall"))
    sensor = Device(id="hall-contact", name="Hall contact", area_id="hall")
    await repository.upsert_device(sensor)
    engine = EpisodeEngine(repository, EventBus(), timeout=30)
    await engine.start()
    router = IngressRouter()
    ingestion = IngestionService(config.data_dir, repository, engine, router)
    registrations = discover_external_plugins(
        tmp_path / "plugins",
        [ExternalPluginConfig(id="shared-example")],
    )
    manager = PluginManager(
        registrations,
        PluginContext(
            plugins_dir=tmp_path / "plugins",
            configured_devices=(asdict(sensor),),
            raw_delivery_sink=RawPluginDeliveryStore(ingestion),
            ingress_router=router,
        ),
    )
    raw = sensor.id.encode()
    try:
        await manager.start()
        outcome = await ingestion.accept(
            IngressDelivery(
                source="http:test",
                transport="http",
                received_at=datetime.now(tz=timezone.utc),
                payload=raw,
            )
        )
        assert outcome.canonical_event is not None
        assert outcome.canonical_event.event.device_id == sensor.id
        assert outcome.canonical_event.event.event_type == "contact"
        artifact = await repository.get_raw_artifact(outcome.receipt.artifact_id)
        assert artifact is not None
        assert Path(artifact.file_path).read_bytes() == raw
    finally:
        await manager.stop()
        await engine.stop()
        await repository.close()


@pytest.mark.asyncio
async def test_external_plugin_missing_dependency_fails_without_remaining_imported(tmp_path):
    plugin_root = tmp_path / "plugins" / "missing-dependency"
    plugin_root.mkdir(parents=True)
    (plugin_root / "episode-plugin.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "missing-dependency",
                "name": "Missing dependency",
                "version": "1.0.0",
                "plugin_api": "1",
                "kind": "ingress",
                "entrypoint": "plugin.py:create_plugin",
            }
        )
    )
    (plugin_root / "plugin.py").write_text(
        "import dependency_that_episode_does_not_install\n\n"
        "def create_plugin(context):\n    raise AssertionError('unreachable')\n"
    )
    registrations = discover_external_plugins(
        tmp_path / "plugins",
        [ExternalPluginConfig(id="missing-dependency")],
    )

    async def raw_delivery_sink(_delivery):
        raise AssertionError("unreachable")

    manager = PluginManager(
        registrations,
        PluginContext(
            tmp_path / "plugins",
            raw_delivery_sink=raw_delivery_sink,
            ingress_router=IngressRouter(),
        ),
    )

    await manager.start()

    status = manager.statuses()[0]
    assert status["state"] == PluginState.FAILED
    assert status["error"] == "Plugin startup failed. See the Episode log for details."
    assert not any(name.startswith("_episode_external_missing_dependency") for name in sys.modules)


@pytest.mark.asyncio
async def test_external_plugin_startup_timeout_removes_registered_resources(tmp_path):
    plugin_root = tmp_path / "plugins" / "partial-external"
    plugin_root.mkdir(parents=True)
    (plugin_root / "episode-plugin.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "partial-external",
                "name": "Partial external",
                "version": "1.0.0",
                "plugin_api": "1",
                "kind": "device",
                "entrypoint": "plugin.py:create_plugin",
            }
        )
    )
    (plugin_root / "plugin.py").write_text(
        """import asyncio
from episode.plugin_api import (
    HandlerRegistration, HandlerResult, MediaSource, PluginState, PluginStatus,
)

async def handle(delivery):
    return HandlerResult()

class Plugin:
    def __init__(self, context):
        self.context = context
    async def start(self):
        self.context.ingress.register(HandlerRegistration(
            id="events", matcher=lambda delivery: True,
            handler=handle))
        self.context.media.register(MediaSource(
            device_id=self.context.devices[0].id,
            stream_uri="rtsp://192.0.2.10/live"))
        await asyncio.Event().wait()
    async def stop(self):
        pass
    def status(self):
        return PluginStatus(state=PluginState.READY)

def create_plugin(context):
    return Plugin(context)
"""
    )
    camera = Device(id="camera", name="Camera", device_type="camera", area_id="garden")
    router = IngressRouter()
    media = MediaRegistry()
    registrations = discover_external_plugins(
        tmp_path / "plugins",
        [ExternalPluginConfig(id="partial-external", device_ids=[camera.id])],
    )
    manager = PluginManager(
        registrations,
        PluginContext(
            tmp_path / "plugins",
            configured_devices=(asdict(camera),),
            raw_delivery_sink=lambda _delivery: None,
            ingress_router=router,
            media_registry=media,
        ),
        startup_timeout=0.01,
    )

    await manager.start()

    status = manager.statuses()[0]
    assert status["state"] == PluginState.FAILED
    assert status["error"] == "Plugin startup timed out after 0.01s."
    assert router.status("external:partial-external:events") is None
    assert media.get(camera.id) is None


@pytest.mark.asyncio
async def test_external_plugin_factory_failure_removes_registered_resources(tmp_path):
    plugin_root = tmp_path / "plugins" / "failed-factory"
    plugin_root.mkdir(parents=True)
    (plugin_root / "episode-plugin.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "failed-factory",
                "name": "Failed factory",
                "version": "1.0.0",
                "plugin_api": "1",
                "kind": "device",
                "entrypoint": "plugin.py:create_plugin",
            }
        )
    )
    (plugin_root / "plugin.py").write_text(
        """from episode.plugin_api import HandlerRegistration, MediaSource

async def handle(delivery):
    raise AssertionError("unreachable")

def create_plugin(context):
    context.ingress.register(HandlerRegistration(
        id="events", matcher=lambda delivery: True, handler=handle))
    context.media.register(MediaSource(
        device_id=context.devices[0].id, stream_uri="rtsp://192.0.2.10/live"))
    raise RuntimeError("factory failed")
"""
    )
    camera = Device(id="camera", name="Camera", device_type="camera", area_id="garden")
    router = IngressRouter()
    media = MediaRegistry()
    registrations = discover_external_plugins(
        tmp_path / "plugins",
        [ExternalPluginConfig(id="failed-factory", device_ids=[camera.id])],
    )

    async def raw_delivery_sink(_delivery):
        raise AssertionError("unreachable")

    manager = PluginManager(
        registrations,
        PluginContext(
            tmp_path / "plugins",
            configured_devices=(asdict(camera),),
            raw_delivery_sink=raw_delivery_sink,
            ingress_router=router,
            media_registry=media,
        ),
    )

    await manager.start()

    assert manager.statuses()[0]["state"] == PluginState.FAILED
    assert router.status("external:failed-factory:events") is None
    assert media.get(camera.id) is None


@pytest.mark.asyncio
async def test_external_plugin_system_exit_is_isolated_and_module_removed(tmp_path):
    plugin_root = tmp_path / "plugins" / "terminating-plugin"
    plugin_root.mkdir(parents=True)
    (plugin_root / "episode-plugin.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "terminating-plugin",
                "name": "Terminating plugin",
                "version": "1.0.0",
                "plugin_api": "1",
                "kind": "ingress",
                "entrypoint": "plugin.py:create_plugin",
            }
        )
    )
    (plugin_root / "plugin.py").write_text("raise SystemExit(12)\n")
    registrations = discover_external_plugins(
        tmp_path / "plugins",
        [ExternalPluginConfig(id="terminating-plugin")],
    )

    async def raw_delivery_sink(_delivery):
        raise AssertionError("unreachable")

    manager = PluginManager(
        registrations,
        PluginContext(
            tmp_path / "plugins",
            raw_delivery_sink=raw_delivery_sink,
            ingress_router=IngressRouter(),
        ),
    )

    await manager.start()

    assert manager.statuses()[0]["state"] == PluginState.FAILED
    assert not any(name.startswith("_episode_external_terminating_plugin") for name in sys.modules)
