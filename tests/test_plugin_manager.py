from __future__ import annotations

import asyncio
import json
import sys

import httpx
import pytest
from fastapi import FastAPI

from episode.__main__ import Application
from episode.config import EpisodeConfig
from episode.domain.models import Area, CapabilityConfig, Device
from episode.plugins.api import register_plugins_api
from episode.plugins.manager import PluginManager
from episode.plugins.models import (
    PluginContext,
    PluginRegistration,
    PluginState,
    PluginStatus,
)
from episode.plugins.probe import PROBE_RESULT_PREFIX, SubprocessProbeRunner
from episode.plugins.registry import PluginRegistry, builtin_plugin_registry


class FakePlugin:
    def __init__(
        self,
        plugin_id: str,
        name: str,
        kind: str,
        events: list[str],
        state: PluginState = PluginState.READY,
    ):
        self._status = PluginStatus(plugin_id, name, kind, state)
        self._events = events

    def status(self) -> PluginStatus:
        return self._status

    async def start(self) -> None:
        self._events.append(f"start:{self._status.id}")

    async def stop(self) -> None:
        self._events.append(f"stop:{self._status.id}")


def _registration(plugin_id: str, capability: str, events: list[str]) -> PluginRegistration:
    name = f"Plugin {plugin_id}"

    def factory(_context):
        events.append(f"load:{plugin_id}")
        return FakePlugin(plugin_id, name, "test", events)

    return PluginRegistration(plugin_id, name, "test", capability, factory)


def _python_command(source: str) -> list[str]:
    return [sys.executable, "-c", source]


@pytest.mark.asyncio
async def test_only_configured_plugin_is_loaded_from_large_registry(tmp_path):
    events: list[str] = []
    registrations = [
        _registration(f"plugin-{index}", f"capability-{index}", events) for index in range(3000)
    ]
    registry = PluginRegistry(registrations)
    selected = registry.for_configuration({"capability-1729"})

    assert [registration.id for registration in selected] == ["plugin-1729"]
    assert events == []

    manager = PluginManager(selected, PluginContext(tmp_path))
    await manager.start()
    await manager.stop()

    assert events == ["load:plugin-1729", "start:plugin-1729", "stop:plugin-1729"]


def test_builtin_plugin_module_is_not_imported_during_registration(monkeypatch):
    imported: list[str] = []

    def track_import(module_name):
        imported.append(module_name)
        raise AssertionError("plugin module should remain unloaded")

    monkeypatch.setattr("episode.plugins.registry.importlib.import_module", track_import)
    registry = builtin_plugin_registry()

    validators = registry.validators()
    assert set(validators) == {"onvif", "isapi"}
    assert imported == []
    selected = registry.for_configuration({"onvif", "video"})
    assert [registration.id for registration in selected] == ["onvif"]
    selected = registry.for_configuration({"isapi"})
    assert [registration.id for registration in selected] == ["hikvision-isapi"]
    selected = registry.for_configuration({"hikvision_sdk"})
    assert [registration.id for registration in selected] == ["hikvision-sdk"]
    assert imported == []


@pytest.mark.asyncio
async def test_importing_one_hikvision_plugin_does_not_load_its_siblings():
    source = (
        "import importlib,json,sys;"
        "importlib.import_module('episode.plugins.hikvision.isapi');"
        "siblings=['episode.plugins.hikvision.alarm_server',"
        "'episode.plugins.hikvision.ftp','episode.plugins.hikvision.sdk'];"
        f"print({PROBE_RESULT_PREFIX!r}+json.dumps({{"
        "'ok':True,'loaded':[name for name in siblings if name in sys.modules]}))"
    )
    runner = SubprocessProbeRunner()

    result = await runner.run(_python_command(source))

    assert result.succeeded
    assert result.payload == {"ok": True, "loaded": []}


def test_shared_connector_activates_only_its_registered_handler_plugin():
    registry = builtin_plugin_registry()

    selected = registry.for_configuration(set(), {"alarm_server"})

    assert [registration.id for registration in selected] == ["hikvision-alarm-server"]

    selected = registry.for_configuration(set(), {"ftp"})

    assert [registration.id for registration in selected] == ["hikvision-ftp"]


def test_application_ignores_installed_plugin_without_device_capability(tmp_path):
    sdk_dir = tmp_path / "plugins" / "hikvision-sdk"
    sdk_dir.mkdir(parents=True)
    (sdk_dir / "junk.so").write_bytes(b"not a plugin")
    config = EpisodeConfig(
        data_dir=str(tmp_path / "data"),
        plugins_dir=str(tmp_path / "plugins"),
    )

    application = Application(config)

    assert application._plugins.statuses() == []


def test_device_capability_configures_plugin_without_importing_it(tmp_path, monkeypatch):
    imported: list[str] = []

    def track_import(module_name):
        imported.append(module_name)
        raise AssertionError("plugin should not be imported during application construction")

    monkeypatch.setattr("episode.plugins.registry.importlib.import_module", track_import)
    manager = PluginManager(
        builtin_plugin_registry().for_configuration({"hikvision_sdk"}),
        PluginContext(tmp_path),
    )

    assert manager.statuses()[0]["id"] == "hikvision-sdk"
    assert manager.statuses()[0]["state"] == PluginState.VALIDATING
    assert manager.statuses()[0]["integration"]["type"] == "hikvision_sdk"
    assert imported == []


def test_plugin_context_can_carry_device_configuration_without_manager_coupling(tmp_path):
    device = {"id": "doorbell", "configs": {"hikvision_sdk": {}}}
    context = PluginContext(tmp_path, (device,))

    assert context.configured_devices == (device,)


@pytest.mark.asyncio
async def test_application_reloads_saved_inventory_without_process_restart(tmp_path):
    application = Application(EpisodeConfig(data_dir=str(tmp_path)))
    events: list[str] = []
    application._plugin_registry.register(_registration("test-device", "test-device", events))
    await application._repo.initialize()
    try:
        await application._inventory.save_area(Area(id="gate", name="Gate"), create=True)
        await application._inventory.save_device(
            Device(
                id="gate-camera",
                name="Gate camera",
                device_type="camera",
                area_id="gate",
                configs={"test-device": CapabilityConfig()},
            ),
            create=True,
        )

        assert [status["id"] for status in application._plugins.statuses()] == ["test-device"]
        assert events == ["load:test-device", "start:test-device"]
    finally:
        await application._plugins.stop()
        await application._repo.close()


@pytest.mark.asyncio
async def test_application_reloads_device_integrations_during_recording(tmp_path):
    application = Application(EpisodeConfig(data_dir=str(tmp_path)))
    events: list[str] = []
    application._plugin_registry.register(_registration("test-device", "test-device", events))
    await application._repo.initialize()
    try:
        await application._inventory.save_area(Area(id="gate", name="Gate"), create=True)
        application._recorder.status = lambda: {"active_recordings": 1}
        await application._inventory.save_device(
            Device(
                id="gate-camera",
                name="Gate camera",
                device_type="camera",
                area_id="gate",
                configs={"test-device": CapabilityConfig()},
            ),
            create=True,
        )
        assert [status["id"] for status in application._plugins.statuses()] == ["test-device"]
        assert events == ["load:test-device", "start:test-device"]
    finally:
        await application._plugins.stop()
        await application._repo.close()


def test_duplicate_plugin_registration_is_rejected():
    events: list[str] = []
    registration = _registration("duplicate", "duplicate_capability", events)

    with pytest.raises(ValueError, match="already registered"):
        PluginRegistry([registration, registration])


@pytest.mark.asyncio
async def test_plugin_startup_failure_does_not_block_other_plugins(tmp_path):
    events: list[str] = []

    def broken_factory(_context):
        raise RuntimeError("broken factory")

    broken = PluginRegistration("broken", "Broken", "test", "broken", broken_factory)
    healthy = _registration("healthy", "healthy", events)
    manager = PluginManager([broken, healthy], PluginContext(tmp_path))

    await manager.start()

    statuses = manager.statuses()
    assert statuses[0]["state"] == PluginState.FAILED
    assert statuses[0]["error"] == "Plugin startup failed. See the Episode log for details."
    assert statuses[1]["state"] == PluginState.READY
    assert events == ["load:healthy", "start:healthy"]


@pytest.mark.asyncio
async def test_plugin_startup_timeout_cleans_up_and_does_not_block_other_plugins(tmp_path):
    events: list[str] = []

    class HangingPlugin(FakePlugin):
        async def start(self) -> None:
            events.append("start:hanging")
            await asyncio.Event().wait()

        async def stop(self) -> None:
            events.append("stop:hanging")

    hanging = PluginRegistration(
        "hanging",
        "Hanging",
        "test",
        "hanging",
        lambda _context: HangingPlugin("hanging", "Hanging", "test", events),
    )
    healthy = _registration("healthy", "healthy", events)
    manager = PluginManager(
        [hanging, healthy],
        PluginContext(tmp_path),
        startup_timeout=0.01,
    )

    await manager.start()

    statuses = manager.statuses()
    assert statuses[0]["state"] == PluginState.FAILED
    assert statuses[0]["error"] == "Plugin startup timed out after 0.01s."
    assert statuses[1]["state"] == PluginState.READY
    assert events == [
        "start:hanging",
        "stop:hanging",
        "load:healthy",
        "start:healthy",
    ]

    await manager.stop()


@pytest.mark.asyncio
async def test_plugin_internal_timeout_is_not_misreported_as_lifecycle_timeout(tmp_path):
    events: list[str] = []

    class InternalTimeoutPlugin(FakePlugin):
        async def start(self) -> None:
            raise TimeoutError("plugin operation timed out")

    registration = PluginRegistration(
        "internal-timeout",
        "Internal timeout",
        "test",
        "internal-timeout",
        lambda _context: InternalTimeoutPlugin(
            "internal-timeout", "Internal timeout", "test", events
        ),
    )
    manager = PluginManager([registration], PluginContext(tmp_path), startup_timeout=1)

    await manager.start()

    status = manager.statuses()[0]
    assert status["state"] == PluginState.FAILED
    assert status["error"] == "Plugin startup failed. See the Episode log for details."


@pytest.mark.asyncio
async def test_plugin_cancellation_is_isolated_from_other_plugins(tmp_path):
    events: list[str] = []

    class CancellingPlugin(FakePlugin):
        async def start(self) -> None:
            raise asyncio.CancelledError

    cancelling = PluginRegistration(
        "cancelling",
        "Cancelling",
        "test",
        "cancelling",
        lambda _context: CancellingPlugin("cancelling", "Cancelling", "test", events),
    )
    healthy = _registration("healthy", "healthy", events)
    manager = PluginManager([cancelling, healthy], PluginContext(tmp_path))

    await manager.start()

    statuses = manager.statuses()
    assert statuses[0]["state"] == PluginState.FAILED
    assert statuses[1]["state"] == PluginState.READY
    assert "start:healthy" in events

    await manager.stop()


@pytest.mark.asyncio
async def test_plugin_shutdown_timeout_does_not_skip_remaining_plugins(tmp_path):
    events: list[str] = []

    class HangingStopPlugin(FakePlugin):
        async def stop(self) -> None:
            events.append("stop:hanging")
            await asyncio.Event().wait()

    healthy = _registration("healthy", "healthy", events)
    hanging = PluginRegistration(
        "hanging",
        "Hanging",
        "test",
        "hanging",
        lambda _context: HangingStopPlugin("hanging", "Hanging", "test", events),
    )
    manager = PluginManager(
        [healthy, hanging],
        PluginContext(tmp_path),
        shutdown_timeout=0.01,
    )
    await manager.start()

    await manager.stop()

    assert events[-2:] == ["stop:hanging", "stop:healthy"]


@pytest.mark.asyncio
async def test_non_cooperative_plugin_cannot_block_shutdown_timeout(tmp_path):
    events: list[str] = []
    release = asyncio.Event()

    class NonCooperativePlugin(FakePlugin):
        async def stop(self) -> None:
            events.append("stop:non-cooperative")
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue

    healthy = _registration("healthy", "healthy", events)
    non_cooperative = PluginRegistration(
        "non-cooperative",
        "Non-cooperative",
        "test",
        "non-cooperative",
        lambda _context: NonCooperativePlugin("non-cooperative", "Non-cooperative", "test", events),
    )
    manager = PluginManager(
        [healthy, non_cooperative],
        PluginContext(tmp_path),
        shutdown_timeout=0.01,
    )
    await manager.start()

    await asyncio.wait_for(manager.stop(), timeout=0.2)

    assert events[-2:] == ["stop:non-cooperative", "stop:healthy"]
    release.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_partially_started_plugin_is_stopped_and_remains_failed(tmp_path):
    events: list[str] = []

    class PartialPlugin(FakePlugin):
        async def start(self) -> None:
            events.append("start:partial")
            raise RuntimeError("failed after allocating resources")

    registration = PluginRegistration(
        "partial",
        "Partial",
        "test",
        "partial",
        lambda _context: PartialPlugin("partial", "Partial", "test", events),
    )
    manager = PluginManager([registration], PluginContext(tmp_path))

    await manager.start()
    first = manager.statuses()[0]
    second = manager.statuses()[0]

    assert events == ["start:partial", "stop:partial"]
    assert first["state"] == PluginState.FAILED
    assert second["state"] == PluginState.FAILED


@pytest.mark.asyncio
async def test_plugin_status_failure_is_isolated_from_other_plugins(tmp_path):
    events: list[str] = []

    class UnstablePlugin(FakePlugin):
        def __init__(self):
            super().__init__("unstable", "Unstable", "test", events)
            self._status_calls = 0

        def status(self) -> PluginStatus:
            self._status_calls += 1
            if self._status_calls > 1:
                raise RuntimeError("status failed")
            return super().status()

    unstable = PluginRegistration(
        "unstable",
        "Unstable",
        "test",
        "unstable",
        lambda _context: UnstablePlugin(),
    )
    healthy = _registration("healthy", "healthy", events)
    manager = PluginManager([unstable, healthy], PluginContext(tmp_path))

    await manager.start()
    statuses = manager.statuses()
    await manager.stop()

    assert statuses[0]["state"] == PluginState.FAILED
    assert statuses[0]["error"] == (
        "Plugin status is unavailable. See the Episode log for details."
    )
    assert statuses[1]["state"] == PluginState.READY
    assert "stop:healthy" in events


@pytest.mark.asyncio
async def test_plugins_api_lists_only_configured_plugins(tmp_path):
    manager = PluginManager([], PluginContext(tmp_path))
    app = FastAPI()
    register_plugins_api(app, manager)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/plugins")

    assert response.status_code == 200
    assert response.json() == []
    assert str(tmp_path) not in response.text


@pytest.mark.asyncio
async def test_generic_probe_accepts_clean_structured_result():
    payload = json.dumps({"ok": True, "value": "ready"})
    runner = SubprocessProbeRunner()

    result = await runner.run(_python_command(f"print({PROBE_RESULT_PREFIX + payload!r})"))

    assert result.succeeded
    assert result.payload == {"ok": True, "value": "ready"}


@pytest.mark.asyncio
async def test_generic_probe_rejects_success_marker_followed_by_crash():
    payload = json.dumps({"ok": True, "value": "ready"})
    source = f"import os; print({PROBE_RESULT_PREFIX + payload!r}, flush=True); os._exit(9)"
    runner = SubprocessProbeRunner()

    result = await runner.run(_python_command(source))

    assert not result.succeeded
    assert "code 9" in result.error


@pytest.mark.asyncio
async def test_generic_probe_kills_hung_worker():
    runner = SubprocessProbeRunner(timeout=0.05)

    result = await runner.run(_python_command("import time; time.sleep(60)"))

    assert not result.succeeded
    assert result.error == "Plugin validation timed out."


@pytest.mark.asyncio
async def test_cancelled_generic_probe_cleans_up_worker():
    runner = SubprocessProbeRunner()
    run_task = asyncio.create_task(runner.run(_python_command("import time; time.sleep(60)")))
    for _attempt in range(100):
        if runner._process is not None:
            break
        await asyncio.sleep(0.001)

    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task
    await runner.stop()

    assert runner._process is None


@pytest.mark.asyncio
async def test_invalid_public_status_is_replaced_with_failure(tmp_path):
    events: list[str] = []

    class InvalidStatusPlugin(FakePlugin):
        def status(self) -> PluginStatus:
            return PluginStatus(
                "invalid",
                "Invalid",
                "test",
                PluginState.READY,
                metrics={"unsupported": object()},
            )

    registration = PluginRegistration(
        "invalid",
        "Invalid",
        "test",
        "invalid",
        lambda _context: InvalidStatusPlugin("invalid", "Invalid", "test", events),
    )
    manager = PluginManager([registration], PluginContext(tmp_path))

    await manager.start()
    status = manager.statuses()[0]
    await manager.stop()

    assert status["state"] == PluginState.FAILED
    assert status["metrics"] == {}


@pytest.mark.asyncio
async def test_plugin_status_redacts_common_secret_fields(tmp_path):
    events: list[str] = []

    class SensitiveStatusPlugin(FakePlugin):
        def status(self) -> PluginStatus:
            return PluginStatus(
                "sensitive",
                "Sensitive",
                "test",
                PluginState.READY,
                metrics={
                    "password": "camera-password",
                    "nested": {
                        "api-key": "service-key",
                        "auth_token": "service-token",
                        "password_configured": True,
                        "token": "Profile_1",
                    },
                },
            )

    registration = PluginRegistration(
        "sensitive",
        "Sensitive",
        "test",
        "sensitive",
        lambda _context: SensitiveStatusPlugin("sensitive", "Sensitive", "test", events),
    )
    manager = PluginManager([registration], PluginContext(tmp_path))

    await manager.start()
    status = manager.statuses()[0]
    await manager.stop()

    assert status["metrics"] == {
        "password": "[redacted]",
        "nested": {
            "api-key": "[redacted]",
            "auth_token": "[redacted]",
            "password_configured": True,
            "token": "Profile_1",
        },
    }
