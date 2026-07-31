from __future__ import annotations

import os
import platform
import re
import sys
from pathlib import Path
from typing import Callable, Sequence

from episode.plugins.models import PluginContext, PluginState, PluginStatus
from episode.plugins.probe import SubprocessProbeRunner

PLUGIN_ID = "hikvision-sdk"
PLUGIN_NAME = "Hikvision HCNetSDK"
PLUGIN_KIND = "native-sdk"
_VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+\.\d+$")

_REQUIRED_SDK_FILES = (
    "libhcnetsdk.so",
    "libHCCore.so",
    "libhpr.so",
    "HCNetSDKCom/libHCAlarm.so",
)
_REQUIRED_SDK_DIRECTORIES = ("HCNetSDKCom",)

ProbeCommand = Callable[[Path], Sequence[str]]


def normalize_architecture(machine: str) -> str | None:
    normalized = machine.lower().replace("-", "_")
    if normalized in {"x86_64", "amd64"}:
        return "amd64"
    if normalized in {"aarch64", "arm64"}:
        return "arm64"
    return None


def read_elf_architecture(library: Path) -> str:
    try:
        with library.open("rb") as stream:
            header = stream.read(20)
    except OSError as exc:
        raise ValueError("The main SDK library is not readable.") from exc

    if len(header) < 20 or header[:4] != b"\x7fELF":
        raise ValueError("The main SDK library is not a valid ELF binary.")
    if header[4] != 2:
        raise ValueError("Only 64-bit SDK libraries are supported.")
    if header[5] not in {1, 2}:
        raise ValueError("The SDK library uses an unsupported ELF byte order.")

    byte_order = "little" if header[5] == 1 else "big"
    machine = int.from_bytes(header[18:20], byte_order)
    architectures = {62: "amd64", 183: "arm64"}
    if machine not in architectures:
        raise ValueError(f"The SDK library uses unsupported ELF machine type {machine}.")
    return architectures[machine]


def inspect_sdk(plugin_path: Path, host_machine: str | None = None) -> PluginStatus:
    common = {"id": PLUGIN_ID, "name": PLUGIN_NAME, "kind": PLUGIN_KIND}
    if not plugin_path.exists():
        return PluginStatus(**common, state=PluginState.NOT_INSTALLED)
    if not plugin_path.is_dir():
        return PluginStatus(
            **common,
            state=PluginState.INCOMPLETE,
            error="The plugin path is not a directory.",
        )

    missing = [
        relative
        for relative in _REQUIRED_SDK_FILES
        if not (plugin_path / relative).is_file() or not os.access(plugin_path / relative, os.R_OK)
    ]
    missing.extend(
        relative for relative in _REQUIRED_SDK_DIRECTORIES if not (plugin_path / relative).is_dir()
    )
    if missing:
        names = ", ".join(missing)
        return PluginStatus(
            **common,
            state=PluginState.INCOMPLETE,
            error=f"Required SDK files are missing or unreadable: {names}.",
        )

    try:
        sdk_arch = read_elf_architecture(plugin_path / "libhcnetsdk.so")
    except ValueError as exc:
        return PluginStatus(
            **common,
            state=PluginState.INCOMPATIBLE,
            error=str(exc),
        )

    host_arch = normalize_architecture(host_machine or platform.machine())
    if host_arch is None:
        return PluginStatus(
            **common,
            state=PluginState.INCOMPATIBLE,
            architecture=sdk_arch,
            error="The host CPU architecture is not supported.",
        )
    if sdk_arch != host_arch:
        return PluginStatus(
            **common,
            state=PluginState.INCOMPATIBLE,
            architecture=sdk_arch,
            error=f"The {sdk_arch} SDK is not compatible with this {host_arch} host.",
        )

    return PluginStatus(
        **common,
        state=PluginState.VALIDATING,
        architecture=sdk_arch,
    )


def _default_probe_command(plugin_path: Path) -> Sequence[str]:
    return [
        sys.executable,
        "-m",
        "episode.plugins.hikvision_sdk.probe",
        str(plugin_path),
    ]


class HikvisionSDKPlugin:
    def __init__(
        self,
        context: PluginContext,
        *,
        runner: SubprocessProbeRunner | None = None,
        probe_command: ProbeCommand | None = None,
        host_machine: str | None = None,
    ):
        self._path = context.plugins_dir / PLUGIN_ID
        self._runner = runner or SubprocessProbeRunner()
        self._probe_command = probe_command or _default_probe_command
        self._host_machine = host_machine
        self._status = PluginStatus(
            id=PLUGIN_ID,
            name=PLUGIN_NAME,
            kind=PLUGIN_KIND,
            state=PluginState.VALIDATING,
        )

    def status(self) -> PluginStatus:
        return self._status

    async def start(self) -> None:
        self._status = inspect_sdk(self._path, self._host_machine)
        if self._status.state != PluginState.VALIDATING:
            return

        library_paths = [str(self._path), str(self._path / "HCNetSDKCom")]
        if os.environ.get("LD_LIBRARY_PATH"):
            library_paths.append(os.environ["LD_LIBRARY_PATH"])
        result = await self._runner.run(
            self._probe_command(self._path),
            environment={"LD_LIBRARY_PATH": os.pathsep.join(library_paths)},
            redact_paths=[self._path],
        )
        version = result.payload.get("version") if result.payload else None
        if result.succeeded and isinstance(version, str) and _VERSION_PATTERN.fullmatch(version):
            self._status = PluginStatus(
                id=PLUGIN_ID,
                name=PLUGIN_NAME,
                kind=PLUGIN_KIND,
                state=PluginState.READY,
                version=version,
                architecture=self._status.architecture,
            )
            return

        error = result.error or "HCNetSDK validation returned an invalid version."
        self._status = PluginStatus(
            id=PLUGIN_ID,
            name=PLUGIN_NAME,
            kind=PLUGIN_KIND,
            state=PluginState.FAILED,
            architecture=self._status.architecture,
            error=error,
        )

    async def stop(self) -> None:
        await self._runner.stop()
