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
            "capabilities": ["hikvision_sdk"],
        },
        {
            "id": "unavailable",
            "name": "Unavailable",
            "area_id": "front-door",
            "ip_address": "192.0.2.11",
            "username": "user",
            "password": "secret-two",
            "capabilities": ["hikvision_sdk"],
            "configs": {"hikvision_sdk": {"port": 9000}},
        },
        {"id": "ordinary-camera", "capabilities": ["video"]},
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
