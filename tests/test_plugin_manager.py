from __future__ import annotations

import asyncio
import json
import sys

import httpx
import pytest
from fastapi import FastAPI

from episode.__main__ import Application
from episode.config import EpisodeConfig
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
    selected = registry.for_capabilities({"capability-1729"})

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
    assert set(validators) == {"isapi"}
    assert imported == []
    assert registry.for_capabilities({"onvif", "video"}) == []
    selected = registry.for_capabilities({"isapi"})
    assert [registration.id for registration in selected] == ["hikvision-isapi"]
    selected = registry.for_capabilities({"hikvision_sdk"})
    assert [registration.id for registration in selected] == ["hikvision-sdk"]
    assert imported == []


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
        devices=[{"id": "camera", "capabilities": ["video"]}],
    )

    application = Application(config)

    assert application._plugins.statuses() == []


def test_device_capability_configures_plugin_without_importing_it(tmp_path, monkeypatch):
    imported: list[str] = []

    def track_import(module_name):
        imported.append(module_name)
        raise AssertionError("plugin should not be imported during application construction")

    monkeypatch.setattr("episode.plugins.registry.importlib.import_module", track_import)
    config = EpisodeConfig(
        data_dir=str(tmp_path / "data"),
        plugins_dir=str(tmp_path / "plugins"),
        devices=[{"id": "doorbell", "capabilities": ["hikvision_sdk"]}],
    )

    application = Application(config)

    assert application._plugins.statuses()[0]["id"] == "hikvision-sdk"
    assert application._plugins.statuses()[0]["state"] == PluginState.VALIDATING
    assert imported == []


def test_plugin_context_can_carry_device_configuration_without_manager_coupling(tmp_path):
    device = {"id": "doorbell", "capabilities": ["hikvision_sdk"]}
    context = PluginContext(tmp_path, (device,))

    assert context.configured_devices == (device,)


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
