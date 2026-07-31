from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from episode.plugins.api import register_plugins_api
from episode.plugins.manager import (
    PROBE_RESULT_PREFIX,
    NativePluginManager,
    PluginState,
    inspect_hikvision_sdk,
    read_elf_architecture,
)


def _write_elf(path: Path, machine: int = 62) -> None:
    header = bytearray(20)
    header[:4] = b"\x7fELF"
    header[4] = 2
    header[5] = 1
    header[18:20] = machine.to_bytes(2, "little")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header)


def _install_sdk_layout(plugins_dir: Path, machine: int = 62) -> Path:
    sdk_dir = plugins_dir / "hikvision-sdk"
    _write_elf(sdk_dir / "libhcnetsdk.so", machine)
    (sdk_dir / "libHCCore.so").write_bytes(b"placeholder")
    (sdk_dir / "libhpr.so").write_bytes(b"placeholder")
    (sdk_dir / "HCNetSDKCom").mkdir()
    (sdk_dir / "HCNetSDKCom" / "libHCAlarm.so").write_bytes(b"placeholder")
    return sdk_dir


def _python_command(source: str):
    return lambda _plugin_path: [sys.executable, "-c", source]


def test_missing_sdk_is_optional(tmp_path):
    status = inspect_hikvision_sdk(tmp_path, "x86_64")

    assert status.state == PluginState.NOT_INSTALLED
    assert status.error is None


def test_incomplete_sdk_reports_missing_runtime_files(tmp_path):
    sdk_dir = tmp_path / "hikvision-sdk"
    sdk_dir.mkdir()
    _write_elf(sdk_dir / "libhcnetsdk.so")

    status = inspect_hikvision_sdk(tmp_path, "x86_64")

    assert status.state == PluginState.INCOMPLETE
    assert "libHCCore.so" in status.error
    assert "HCNetSDKCom/libHCAlarm.so" in status.error


def test_wrong_sdk_architecture_is_rejected_before_loading(tmp_path):
    _install_sdk_layout(tmp_path, machine=183)

    status = inspect_hikvision_sdk(tmp_path, "x86_64")

    assert status.state == PluginState.INCOMPATIBLE
    assert status.architecture == "arm64"
    assert "amd64 host" in status.error


def test_invalid_sdk_library_is_rejected_before_loading(tmp_path):
    sdk_dir = _install_sdk_layout(tmp_path)
    (sdk_dir / "libhcnetsdk.so").write_bytes(b"not an ELF library")

    status = inspect_hikvision_sdk(tmp_path, "x86_64")

    assert status.state == PluginState.INCOMPATIBLE
    assert "valid ELF" in status.error


def test_elf_architecture_reader_supports_release_targets(tmp_path):
    amd64 = tmp_path / "amd64.so"
    arm64 = tmp_path / "arm64.so"
    _write_elf(amd64, 62)
    _write_elf(arm64, 183)

    assert read_elf_architecture(amd64) == "amd64"
    assert read_elf_architecture(arm64) == "arm64"


@pytest.mark.asyncio
async def test_successful_probe_reports_version(tmp_path):
    _install_sdk_layout(tmp_path)
    payload = json.dumps({"ok": True, "version": "6.1.9.48"})
    manager = NativePluginManager(
        tmp_path,
        host_machine="x86_64",
        probe_command=_python_command(f"print({PROBE_RESULT_PREFIX + payload!r})"),
    )

    await manager.start()

    assert manager.statuses() == [
        {
            "id": "hikvision-sdk",
            "name": "Hikvision HCNetSDK",
            "kind": "native-sdk",
            "state": PluginState.READY,
            "version": "6.1.9.48",
            "architecture": "amd64",
            "error": None,
        }
    ]


@pytest.mark.asyncio
async def test_probe_crash_becomes_failed_state(tmp_path):
    _install_sdk_layout(tmp_path)
    manager = NativePluginManager(
        tmp_path,
        host_machine="x86_64",
        probe_command=_python_command("import os; os._exit(7)"),
    )

    await manager.start()

    status = manager.statuses()[0]
    assert status["state"] == PluginState.FAILED
    assert "code 7" in status["error"]


@pytest.mark.asyncio
async def test_probe_success_marker_followed_by_crash_is_not_ready(tmp_path):
    _install_sdk_layout(tmp_path)
    payload = json.dumps({"ok": True, "version": "6.1.9.48"})
    source = f"import os; print({PROBE_RESULT_PREFIX + payload!r}, flush=True); os._exit(9)"
    manager = NativePluginManager(
        tmp_path,
        host_machine="x86_64",
        probe_command=_python_command(source),
    )

    await manager.start()

    status = manager.statuses()[0]
    assert status["state"] == PluginState.FAILED
    assert "code 9" in status["error"]


@pytest.mark.asyncio
async def test_probe_hang_is_killed_and_becomes_failed_state(tmp_path):
    _install_sdk_layout(tmp_path)
    manager = NativePluginManager(
        tmp_path,
        host_machine="x86_64",
        probe_timeout=0.05,
        probe_command=_python_command("import time; time.sleep(60)"),
    )

    await manager.start()

    status = manager.statuses()[0]
    assert status["state"] == PluginState.FAILED
    assert status["error"] == "Native SDK validation timed out."


@pytest.mark.asyncio
async def test_invalid_probe_output_becomes_failed_state(tmp_path):
    _install_sdk_layout(tmp_path)
    manager = NativePluginManager(
        tmp_path,
        host_machine="x86_64",
        probe_command=_python_command("print('unexpected output')"),
    )

    await manager.start()

    status = manager.statuses()[0]
    assert status["state"] == PluginState.FAILED
    assert status["error"] == "Native SDK validation returned an invalid result."


@pytest.mark.asyncio
async def test_cancelled_probe_cleans_up_worker(tmp_path):
    _install_sdk_layout(tmp_path)
    manager = NativePluginManager(
        tmp_path,
        host_machine="x86_64",
        probe_command=_python_command("import time; time.sleep(60)"),
    )
    start_task = asyncio.create_task(manager.start())
    for _attempt in range(100):
        if manager._process is not None:
            break
        await asyncio.sleep(0.001)

    start_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start_task
    await manager.stop()

    assert manager._process is None


@pytest.mark.asyncio
async def test_plugins_api_is_compact_and_does_not_expose_install_path(tmp_path):
    manager = NativePluginManager(tmp_path)
    app = FastAPI()
    register_plugins_api(app, manager)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/plugins")

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": "hikvision-sdk",
            "name": "Hikvision HCNetSDK",
            "kind": "native-sdk",
            "state": "not_installed",
            "version": None,
            "architecture": None,
            "error": None,
        }
    ]
    assert str(tmp_path) not in response.text
