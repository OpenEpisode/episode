from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from episode.plugins.hikvision.sdk.plugin import HikvisionSDKPlugin
from episode.plugins.models import (
    PluginContext,
    PluginInstanceState,
    PluginInstanceStatus,
    PluginState,
)
from episode.plugins.probe import PROBE_RESULT_PREFIX


def _write_elf(path: Path, machine: int = 62) -> None:
    header = bytearray(20)
    header[:4] = b"\x7fELF"
    header[4] = 2
    header[5] = 1
    header[18:20] = machine.to_bytes(2, "little")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header)


def _install_sdk_layout(plugins_dir: Path) -> None:
    sdk_dir = plugins_dir / "hikvision-sdk"
    _write_elf(sdk_dir / "libhcnetsdk.so")
    (sdk_dir / "libHCCore.so").write_bytes(b"placeholder")
    (sdk_dir / "libhpr.so").write_bytes(b"placeholder")
    (sdk_dir / "HCNetSDKCom").mkdir()
    (sdk_dir / "HCNetSDKCom" / "libHCAlarm.so").write_bytes(b"placeholder")


@pytest.mark.asyncio
async def test_plugin_starts_one_worker_per_explicit_sdk_device(tmp_path):
    _install_sdk_layout(tmp_path)
    payload = json.dumps({"ok": True, "version": "6.1.9.48"})
    workers = []

    class FakeWorker:
        def __init__(self, config):
            self.config = config
            self._status = PluginInstanceStatus(
                id=config.id,
                name=config.name,
                state=PluginInstanceState.STARTING,
            )

        def status(self):
            return self._status

        async def start(self):
            state = (
                PluginInstanceState.FAILED
                if self.config.id == "unavailable"
                else PluginInstanceState.RUNNING
            )
            self._status = PluginInstanceStatus(
                id=self.config.id,
                name=self.config.name,
                state=state,
                error="Device unavailable." if state == PluginInstanceState.FAILED else None,
            )
            return state == PluginInstanceState.RUNNING

        async def stop(self):
            pass

    def worker_factory(_path, config, _sink):
        worker = FakeWorker(config)
        workers.append(worker)
        return worker

    async def preserve(_delivery):
        pass

    devices = (
        {
            "id": "doorbell",
            "name": "Doorbell",
            "area_id": "front-door",
            "ip_address": "192.0.2.10",
            "username": "user",
            "password": "secret-one",
            "configs": {"hikvision_sdk": {}},
        },
        {
            "id": "unavailable",
            "name": "Unavailable",
            "area_id": "front-door",
            "ip_address": "192.0.2.11",
            "username": "user",
            "password": "secret-two",
            "configs": {"hikvision_sdk": {"port": 9000}},
        },
        {"id": "ordinary-camera", "configs": {"video": {}}},
    )
    plugin = HikvisionSDKPlugin(
        PluginContext(tmp_path, devices, preserve),
        host_machine="x86_64",
        probe_command=lambda _path: [
            sys.executable,
            "-c",
            f"print({PROBE_RESULT_PREFIX + payload!r})",
        ],
        worker_factory=worker_factory,
    )

    await plugin.start()

    assert [worker.config.id for worker in workers] == ["doorbell", "unavailable"]
    assert [worker.config.port for worker in workers] == [8000, 9000]
    status = plugin.status()
    assert status.state == PluginState.DEGRADED
    assert len(status.instances) == 2
    assert "secret-one" not in repr(status)
    assert "secret-two" not in repr(status)
    await plugin.stop()


@pytest.mark.asyncio
async def test_camera_uses_video_worker_without_subscribing_to_sdk_events(tmp_path):
    _install_sdk_layout(tmp_path)
    payload = json.dumps({"ok": True, "version": "6.1.9.48"})
    event_workers = []
    video_workers = []

    class FakeWorker:
        def __init__(self, config):
            self.config = config

        def status(self):
            return PluginInstanceStatus(
                id=self.config.id,
                name=self.config.name,
                state=PluginInstanceState.RUNNING,
            )

        async def start(self):
            return True

        async def stop(self):
            pass

    def event_factory(_path, config, _sink):
        worker = FakeWorker(config)
        event_workers.append(worker)
        return worker

    def video_factory(_path, config, media_registry):
        worker = FakeWorker(config)
        worker.media_registry = media_registry
        video_workers.append(worker)
        return worker

    async def preserve(_delivery):
        pass

    devices = (
        {
            "id": "garage-camera",
            "name": "Garage Camera",
            "device_type": "camera",
            "area_id": "garage",
            "ip_address": "192.0.2.20",
            "username": "user",
            "password": "camera-secret",
            "configs": {"hikvision_sdk": {}},
        },
        {
            "id": "front-doorbell",
            "name": "Front Doorbell",
            "device_type": "doorbell",
            "area_id": "front",
            "ip_address": "192.0.2.21",
            "username": "user",
            "password": "doorbell-secret",
            "configs": {"hikvision_sdk": {}},
        },
    )
    media_registry = object()
    plugin = HikvisionSDKPlugin(
        PluginContext(tmp_path, devices, preserve, media_registry=media_registry),
        host_machine="x86_64",
        probe_command=lambda _path: [
            sys.executable,
            "-c",
            f"print({PROBE_RESULT_PREFIX + payload!r})",
        ],
        worker_factory=event_factory,
        video_worker_factory=video_factory,
    )

    await plugin.start()

    assert [worker.config.id for worker in event_workers] == ["front-doorbell"]
    assert [worker.config.id for worker in video_workers] == ["garage-camera"]
    assert video_workers[0].media_registry is media_registry
    status = plugin.status()
    assert status.state == PluginState.READY
    assert "camera-secret" not in repr(status)
    assert "doorbell-secret" not in repr(status)
    await plugin.stop()


@pytest.mark.asyncio
async def test_doorbell_registers_snapshot_only_media_after_worker_starts(tmp_path):
    _install_sdk_layout(tmp_path)
    payload = json.dumps({"ok": True, "version": "6.1.9.48"})
    workers = []

    class MediaRegistry:
        def __init__(self):
            self.registered = []
            self.unregistered = []

        def register(self, source):
            self.registered.append(source)

        def unregister(self, device_id, *, source=None):
            self.unregistered.append((device_id, source))

    class FakeWorker:
        def __init__(self, config):
            self.config = config
            self._status = PluginInstanceStatus(
                id=config.id,
                name=config.name,
                state=PluginInstanceState.STARTING,
            )

        @property
        def device_id(self):
            return self.config.id

        def status(self):
            return self._status

        async def start(self):
            self._status = PluginInstanceStatus(
                id=self.config.id,
                name=self.config.name,
                state=PluginInstanceState.RUNNING,
                capabilities=("events", "snapshot"),
            )
            return True

        async def capture_snapshot(self):
            return b"\xff\xd8snapshot\xff\xd9", "image/jpeg"

        async def stop(self):
            pass

    def worker_factory(_path, config, _sink):
        worker = FakeWorker(config)
        workers.append(worker)
        return worker

    async def preserve(_delivery):
        pass

    media_registry = MediaRegistry()
    devices = (
        {
            "id": "front-doorbell",
            "name": "Front Doorbell",
            "device_type": "doorbell",
            "area_id": "front",
            "ip_address": "192.0.2.21",
            "username": "user",
            "password": "doorbell-secret",
            "configs": {"hikvision_sdk": {}},
        },
    )
    plugin = HikvisionSDKPlugin(
        PluginContext(tmp_path, devices, preserve, media_registry=media_registry),
        host_machine="x86_64",
        probe_command=lambda _path: [
            sys.executable,
            "-c",
            f"print({PROBE_RESULT_PREFIX + payload!r})",
        ],
        worker_factory=worker_factory,
    )

    await plugin.start()

    assert len(media_registry.registered) == 1
    source = media_registry.registered[0]
    assert source.device_id == "front-doorbell"
    assert source.source == "hikvision-sdk-snapshot"
    assert source.stream_uri == ""
    assert source.video_handler is None
    assert source.video_source is None
    assert source.snapshot_fetcher is not None
    assert await source.snapshot_fetcher() == (b"\xff\xd8snapshot\xff\xd9", "image/jpeg")

    await plugin.stop()
    assert media_registry.unregistered == [("front-doorbell", "hikvision-sdk-snapshot")]
