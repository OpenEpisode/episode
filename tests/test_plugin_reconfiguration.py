from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from episode.plugins.manager import PluginManager
from episode.plugins.models import PluginContext, PluginRegistration, PluginState, PluginStatus


class _Plugin:
    def __init__(self, plugin_id: str, events: list[str]) -> None:
        self._status = PluginStatus(plugin_id, plugin_id, "test", PluginState.READY)
        self._events = events

    def status(self) -> PluginStatus:
        return self._status

    async def start(self) -> None:
        self._events.append(f"start:{self._status.id}")

    async def stop(self) -> None:
        self._events.append(f"stop:{self._status.id}")


class _BlockingPlugin(_Plugin):
    def __init__(
        self, plugin_id: str, events: list[str], started: asyncio.Event, block: bool
    ) -> None:
        super().__init__(plugin_id, events)
        self._started = started
        self._block = block

    async def start(self) -> None:
        self._events.append(f"start:{self._status.id}")
        self._started.set()
        if self._block:
            await asyncio.Event().wait()


def _registration(
    plugin_id: str,
    events: list[str],
    *,
    activation_config_type: str = "camera",
    inventory_scope: str = "selected",
    inventory_fields: tuple[str, ...] = (),
    factory=None,
) -> PluginRegistration:
    if factory is None:

        def factory(_context: PluginContext):
            return _Plugin(plugin_id, events)

    return PluginRegistration(
        id=plugin_id,
        name=plugin_id,
        kind="test",
        activation_config_type=activation_config_type,
        factory=factory,
        inventory_scope=inventory_scope,
        inventory_fields=inventory_fields,
    )


def _context(tmp_path, *devices: dict) -> PluginContext:
    return PluginContext(tmp_path, tuple(devices))


@pytest.mark.asyncio
async def test_reconcile_noop_does_not_restart_plugins(tmp_path):
    events: list[str] = []
    registration = _registration("camera", events)
    context = _context(tmp_path, {"id": "one", "enabled": True, "configs": {"camera": {}}})
    manager = PluginManager([registration], context)
    await manager.start()

    await manager.reconcile(
        [registration],
        _context(tmp_path, {"id": "one", "enabled": True, "configs": {"camera": {}}}),
    )

    assert events == ["start:camera"]
    await manager.stop()


@pytest.mark.asyncio
async def test_unrelated_device_change_does_not_restart_selected_plugin(tmp_path):
    events: list[str] = []
    registration = _registration("camera", events)
    first = _context(
        tmp_path,
        {"id": "one", "enabled": True, "configs": {"camera": {}}, "metadata": {"v": 1}},
        {"id": "two", "enabled": True, "configs": {"other": {}}, "metadata": {"v": 1}},
    )
    manager = PluginManager([registration], first)
    await manager.start()

    await manager.reconcile(
        [registration],
        _context(
            tmp_path,
            {"id": "one", "enabled": True, "configs": {"camera": {}}, "metadata": {"v": 1}},
            {"id": "two", "enabled": True, "configs": {"other": {}}, "metadata": {"v": 2}},
        ),
    )

    assert events == ["start:camera"]
    await manager.stop()


@pytest.mark.asyncio
async def test_changed_device_id_ignores_unrelated_inventory_drift(tmp_path):
    events: list[str] = []
    registration = _registration("camera", events)
    manager = PluginManager(
        [registration],
        _context(
            tmp_path,
            {"id": "one", "enabled": True, "configs": {"camera": {}}, "metadata": {"v": 1}},
            {"id": "two", "enabled": True, "configs": {"camera": {}}, "metadata": {"v": 1}},
        ),
    )
    await manager.start()

    await manager.reconcile(
        [registration],
        _context(
            tmp_path,
            {"id": "one", "enabled": True, "configs": {"camera": {}}, "metadata": {"v": 2}},
            {"id": "two", "enabled": True, "configs": {"camera": {}}, "metadata": {"v": 1}},
        ),
        changed_device_id="two",
    )

    assert events == ["start:camera"]
    await manager.stop()


@pytest.mark.asyncio
async def test_concurrent_inventory_writes_each_reconcile_their_own_plugin(tmp_path):
    events: list[str] = []
    first = _registration("first", events, activation_config_type="first")
    second = _registration("second", events, activation_config_type="second")
    initial = _context(
        tmp_path,
        {"id": "one", "enabled": True, "configs": {"first": {}}, "name": "old one"},
        {"id": "two", "enabled": True, "configs": {"second": {}}, "name": "old two"},
    )
    manager = PluginManager([first, second], initial)
    await manager.start()

    # Both writes are visible before either save callback acquires the lock.
    saved = _context(
        tmp_path,
        {"id": "one", "enabled": True, "configs": {"first": {}}, "name": "new one"},
        {"id": "two", "enabled": True, "configs": {"second": {}}, "name": "new two"},
    )
    await manager.reconcile([first, second], saved, changed_device_id="two")
    await manager.reconcile([first, second], saved, changed_device_id="one")

    assert events == [
        "start:first",
        "start:second",
        "stop:second",
        "start:second",
        "stop:first",
        "start:first",
    ]
    await manager.stop()


@pytest.mark.asyncio
async def test_inventory_fields_limit_shared_plugin_restart_inputs(tmp_path):
    events: list[str] = []
    registration = _registration(
        "ftp",
        events,
        activation_config_type="",
        inventory_scope="all",
        inventory_fields=("id", "ip_address", "device_type"),
    )
    device = {"id": "one", "ip_address": "192.0.2.1", "device_type": "camera", "metadata": {"v": 1}}
    manager = PluginManager([registration], _context(tmp_path, device))
    await manager.start()

    await manager.reconcile(
        [registration],
        _context(tmp_path, {**device, "metadata": {"v": 2}}),
    )
    assert events == ["start:ftp"]

    await manager.reconcile(
        [registration],
        _context(tmp_path, {**device, "ip_address": "192.0.2.2"}),
    )
    assert events == ["start:ftp", "stop:ftp", "start:ftp"]
    await manager.stop()


@pytest.mark.asyncio
async def test_dotted_inventory_fields_ignore_core_only_edits_and_copy_nested_values(tmp_path):
    events: list[str] = []
    registration = _registration(
        "camera",
        events,
        inventory_fields=("id", "area_id", "configs.camera"),
    )
    device = {
        "id": "one",
        "name": "Original name",
        "area_id": "gate",
        "activity_window_seconds": 30,
        "configs": {"camera": {"settings": {"port": 1}}},
    }
    context = _context(tmp_path, device)
    manager = PluginManager([registration], context)
    await manager.start()

    # Core-only changes are not runtime inputs for this integration.
    await manager.reconcile(
        [registration],
        _context(
            tmp_path,
            {
                **device,
                "name": "Renamed",
                "activity_window_seconds": 90,
            },
        ),
        changed_device_id="one",
    )
    assert events == ["start:camera"]

    # A nested value is copied into the baseline rather than retaining a
    # reference to the context mapping supplied by the caller.
    device["configs"]["camera"]["settings"]["port"] = 2
    await manager.reconcile(
        [registration],
        _context(tmp_path, device),
        changed_device_id="one",
    )
    assert events == ["start:camera", "stop:camera", "start:camera"]
    await manager.stop()


@pytest.mark.asyncio
async def test_selected_inventory_excludes_devices_needing_setup(tmp_path):
    events: list[str] = []
    registration = _registration("camera", events)
    manager = PluginManager(
        [registration],
        _context(
            tmp_path,
            {
                "id": "one",
                "enabled": True,
                "setup_state": "needs_setup",
                "configs": {"camera": {"port": 1}},
            },
        ),
    )
    await manager.start()

    await manager.reconcile(
        [registration],
        _context(
            tmp_path,
            {
                "id": "one",
                "enabled": True,
                "setup_state": "ready",
                "configs": {"camera": {"port": 1}},
            },
        ),
        changed_device_id="one",
    )

    assert events == ["start:camera", "stop:camera", "start:camera"]
    await manager.stop()


@pytest.mark.asyncio
async def test_external_projection_uses_public_device_fields_only(tmp_path):
    events: list[str] = []
    registration = _registration(
        "example.plugin",
        events,
        activation_config_type="",
        inventory_fields=(
            "id",
            "name",
            "device_type",
            "area_id",
            "ip_address",
            "username",
            "password",
            "configs.example.plugin",
        ),
    )
    device = {
        "id": "one",
        "name": "Sensor",
        "device_type": "sensor",
        "area_id": "gate",
        "ip_address": "192.0.2.1",
        "username": "operator",
        "password": "secret",
        "metadata": {"internal": 1},
        "configs": {
            "example.plugin": {"settings": {"mode": "normal"}},
            "video": {"settings": {"recording_source_id": "other"}},
        },
    }
    registration = replace(registration, configured_device_ids=("one",))
    manager = PluginManager([registration], _context(tmp_path, device))
    await manager.start()

    await manager.reconcile(
        [registration],
        _context(
            tmp_path,
            {
                **device,
                "metadata": {"internal": 2},
                "configs": {
                    **device["configs"],
                    "video": {"settings": {"recording_source_id": "new"}},
                },
            },
        ),
        changed_device_id="one",
    )
    assert events == ["start:example.plugin"]

    await manager.reconcile(
        [registration],
        _context(
            tmp_path,
            {
                **device,
                "name": "Renamed",
                "configs": {
                    "example.plugin": {"settings": {"mode": "fast"}},
                    "video": {"settings": {"recording_source_id": "new"}},
                },
            },
        ),
        changed_device_id="one",
    )
    assert events == [
        "start:example.plugin",
        "stop:example.plugin",
        "start:example.plugin",
    ]
    await manager.stop()


@pytest.mark.asyncio
async def test_reconcile_adds_and_removes_only_affected_plugins(tmp_path):
    events: list[str] = []
    first = _registration("first", events)
    second = _registration("second", events)
    manager = PluginManager([first], _context(tmp_path))
    await manager.start()

    await manager.reconcile([first, second], _context(tmp_path))
    assert events == ["start:first", "start:second"]

    await manager.reconcile([second], _context(tmp_path))
    assert events == ["start:first", "start:second", "stop:first"]
    await manager.stop()


@pytest.mark.asyncio
async def test_nested_context_mutation_does_not_mask_a_later_change(tmp_path):
    events: list[str] = []

    def mutating_factory(context: PluginContext):
        context.configured_devices[0]["configs"]["camera"]["nested"]["value"] = 99
        return _Plugin("camera", events)

    registration = _registration("camera", events, factory=mutating_factory)
    device = {
        "id": "one",
        "enabled": True,
        "configs": {"camera": {"nested": {"value": 1}}},
    }
    manager = PluginManager([registration], _context(tmp_path, device))
    await manager.start()

    await manager.reconcile(
        [registration],
        _context(
            tmp_path,
            {"id": "one", "enabled": True, "configs": {"camera": {"nested": {"value": 2}}}},
        ),
    )

    assert events == ["start:camera", "stop:camera", "start:camera"]
    await manager.stop()


@pytest.mark.asyncio
async def test_changed_selected_device_restarts_plugin(tmp_path):
    events: list[str] = []
    registration = _registration("camera", events)
    manager = PluginManager(
        [registration],
        _context(tmp_path, {"id": "one", "enabled": True, "configs": {"camera": {"port": 1}}}),
    )
    await manager.start()

    await manager.reconcile(
        [registration],
        _context(tmp_path, {"id": "one", "enabled": True, "configs": {"camera": {"port": 2}}}),
    )

    assert events == ["start:camera", "stop:camera", "start:camera"]
    await manager.stop()


@pytest.mark.asyncio
async def test_changed_plugin_failure_does_not_stop_unchanged_plugins(tmp_path):
    events: list[str] = []
    stable = _registration("stable", events)

    def broken_factory(_context: PluginContext):
        events.append("load:broken")
        raise RuntimeError("broken")

    broken = _registration("broken", events, factory=broken_factory)
    manager = PluginManager([stable], _context(tmp_path))
    await manager.start()

    await manager.reconcile([stable, broken], _context(tmp_path))

    assert events == ["start:stable", "load:broken"]
    assert [status["id"] for status in manager.statuses()] == ["stable", "broken"]
    assert manager.statuses()[1]["state"] == "failed"
    await manager.stop()


@pytest.mark.asyncio
async def test_registration_change_restarts_without_inventory_change(tmp_path):
    events: list[str] = []
    original = _registration("camera", events)
    changed = replace(original, installed_version="2")
    manager = PluginManager([original], _context(tmp_path))
    await manager.start()

    await manager.reconcile([changed], _context(tmp_path))

    assert events == ["start:camera", "stop:camera", "start:camera"]
    await manager.stop()


@pytest.mark.asyncio
async def test_replacement_factory_with_same_qualname_restarts_plugin(tmp_path):
    events: list[str] = []
    first = _registration("camera", events)
    second = _registration("camera", events)
    assert first.factory.__qualname__ == second.factory.__qualname__
    assert first != second
    manager = PluginManager([first], _context(tmp_path))
    await manager.start()

    await manager.reconcile([second], _context(tmp_path))

    assert events == ["start:camera", "stop:camera", "start:camera"]
    await manager.stop()


@pytest.mark.asyncio
async def test_cancelled_changed_start_is_retried_without_stopping_unchanged_plugins(tmp_path):
    events: list[str] = []
    stable = _registration("stable", events)
    original = _registration("camera", events)
    started = asyncio.Event()
    calls = 0

    def replacement_factory(_context: PluginContext):
        nonlocal calls
        calls += 1
        return _BlockingPlugin("camera", events, started, block=calls == 1)

    replacement = replace(original, factory=replacement_factory, installed_version="2")
    manager = PluginManager([stable, original], _context(tmp_path))
    await manager.start()

    task = asyncio.create_task(manager.reconcile([stable, replacement], _context(tmp_path)))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert manager.statuses()[1]["state"] == PluginState.FAILED
    assert "interrupted" in manager.statuses()[1]["error"]

    await manager.reconcile([stable, replacement], _context(tmp_path))

    assert events == [
        "start:stable",
        "start:camera",
        "stop:camera",
        "start:camera",
        "stop:camera",
        "start:camera",
    ]
    await manager.stop()
