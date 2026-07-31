from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from episode.plugins.hikvision_sdk.plugin import (
    HikvisionSDKPlugin,
    inspect_sdk,
    read_elf_architecture,
)
from episode.plugins.models import PluginContext, PluginState
from episode.plugins.probe import PROBE_RESULT_PREFIX, SubprocessProbeRunner


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
    status = inspect_sdk(tmp_path / "hikvision-sdk", "x86_64")

    assert status.state == PluginState.NOT_INSTALLED
    assert status.error is None


def test_incomplete_sdk_reports_missing_runtime_files(tmp_path):
    sdk_dir = tmp_path / "hikvision-sdk"
    sdk_dir.mkdir()
    _write_elf(sdk_dir / "libhcnetsdk.so")

    status = inspect_sdk(sdk_dir, "x86_64")

    assert status.state == PluginState.INCOMPLETE
    assert "libHCCore.so" in status.error
    assert "HCNetSDKCom/libHCAlarm.so" in status.error


def test_wrong_sdk_architecture_is_rejected_before_loading(tmp_path):
    sdk_dir = _install_sdk_layout(tmp_path, machine=183)

    status = inspect_sdk(sdk_dir, "x86_64")

    assert status.state == PluginState.INCOMPATIBLE
    assert status.architecture == "arm64"
    assert "amd64 host" in status.error


def test_invalid_sdk_library_is_rejected_before_loading(tmp_path):
    sdk_dir = _install_sdk_layout(tmp_path)
    (sdk_dir / "libhcnetsdk.so").write_bytes(b"not an ELF library")

    status = inspect_sdk(sdk_dir, "x86_64")

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
async def test_successful_sdk_probe_reports_version(tmp_path):
    _install_sdk_layout(tmp_path)
    payload = json.dumps({"ok": True, "version": "6.1.9.48"})
    plugin = HikvisionSDKPlugin(
        PluginContext(tmp_path),
        host_machine="x86_64",
        probe_command=_python_command(f"print({PROBE_RESULT_PREFIX + payload!r})"),
    )

    await plugin.start()

    status = plugin.status()
    assert status.state == PluginState.READY
    assert status.version == "6.1.9.48"
    assert status.architecture == "amd64"


@pytest.mark.asyncio
async def test_sdk_probe_crash_becomes_failed_state(tmp_path):
    _install_sdk_layout(tmp_path)
    plugin = HikvisionSDKPlugin(
        PluginContext(tmp_path),
        host_machine="x86_64",
        probe_command=_python_command("import os; os._exit(7)"),
    )

    await plugin.start()

    status = plugin.status()
    assert status.state == PluginState.FAILED
    assert "code 7" in status.error


@pytest.mark.asyncio
async def test_sdk_probe_hang_becomes_failed_state(tmp_path):
    _install_sdk_layout(tmp_path)
    plugin = HikvisionSDKPlugin(
        PluginContext(tmp_path),
        host_machine="x86_64",
        runner=SubprocessProbeRunner(timeout=0.05),
        probe_command=_python_command("import time; time.sleep(60)"),
    )

    await plugin.start()

    status = plugin.status()
    assert status.state == PluginState.FAILED
    assert status.error == "Plugin validation timed out."


@pytest.mark.asyncio
async def test_invalid_sdk_probe_version_becomes_failed_state(tmp_path):
    _install_sdk_layout(tmp_path)
    payload = json.dumps({"ok": True, "version": "not-a-version"})
    plugin = HikvisionSDKPlugin(
        PluginContext(tmp_path),
        host_machine="x86_64",
        probe_command=_python_command(f"print({PROBE_RESULT_PREFIX + payload!r})"),
    )

    await plugin.start()

    status = plugin.status()
    assert status.state == PluginState.FAILED
    assert status.error == "HCNetSDK validation returned an invalid version."
