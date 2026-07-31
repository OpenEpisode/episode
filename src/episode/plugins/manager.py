from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import re
import sys
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Callable, Sequence

logger = logging.getLogger(__name__)

HIKVISION_PLUGIN_ID = "hikvision-sdk"
HIKVISION_PLUGIN_NAME = "Hikvision HCNetSDK"
PROBE_RESULT_PREFIX = "EPISODE_PLUGIN_RESULT="
_VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+\.\d+$")

_REQUIRED_SDK_FILES = (
    "libhcnetsdk.so",
    "libHCCore.so",
    "libhpr.so",
    "HCNetSDKCom/libHCAlarm.so",
)
_REQUIRED_SDK_DIRECTORIES = ("HCNetSDKCom",)


class PluginState(StrEnum):
    NOT_INSTALLED = "not_installed"
    INCOMPLETE = "incomplete"
    INCOMPATIBLE = "incompatible"
    VALIDATING = "validating"
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True)
class PluginStatus:
    id: str
    name: str
    kind: str
    state: PluginState
    version: str | None = None
    architecture: str | None = None
    error: str | None = None

    def public(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ProbeResult:
    version: str | None = None
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.version is not None and self.error is None


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


def inspect_hikvision_sdk(plugin_dir: Path, host_machine: str | None = None) -> PluginStatus:
    base = plugin_dir / HIKVISION_PLUGIN_ID
    common = {
        "id": HIKVISION_PLUGIN_ID,
        "name": HIKVISION_PLUGIN_NAME,
        "kind": "native-sdk",
    }
    if not base.exists():
        return PluginStatus(**common, state=PluginState.NOT_INSTALLED)
    if not base.is_dir():
        return PluginStatus(
            **common,
            state=PluginState.INCOMPLETE,
            error="The plugin path is not a directory.",
        )

    missing = [
        relative
        for relative in _REQUIRED_SDK_FILES
        if not (base / relative).is_file() or not os.access(base / relative, os.R_OK)
    ]
    missing.extend(
        relative for relative in _REQUIRED_SDK_DIRECTORIES if not (base / relative).is_dir()
    )
    if missing:
        names = ", ".join(missing)
        return PluginStatus(
            **common,
            state=PluginState.INCOMPLETE,
            error=f"Required SDK files are missing or unreadable: {names}.",
        )

    try:
        sdk_arch = read_elf_architecture(base / "libhcnetsdk.so")
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
    return [sys.executable, "-m", "episode.plugins.hikvision_sdk_probe", str(plugin_path)]


class NativePluginManager:
    def __init__(
        self,
        plugins_dir: str | Path,
        *,
        probe_timeout: float = 10.0,
        probe_command: ProbeCommand | None = None,
        host_machine: str | None = None,
    ):
        self._plugins_dir = Path(plugins_dir)
        self._probe_timeout = probe_timeout
        self._probe_command = probe_command or _default_probe_command
        self._host_machine = host_machine
        self._status = inspect_hikvision_sdk(self._plugins_dir, host_machine)
        self._process: asyncio.subprocess.Process | None = None

    def statuses(self) -> list[dict]:
        return [self._status.public()]

    async def start(self) -> None:
        self._status = inspect_hikvision_sdk(self._plugins_dir, self._host_machine)
        if self._status.state == PluginState.NOT_INSTALLED:
            logger.info("Native plugin %s is not installed", HIKVISION_PLUGIN_ID)
            return
        if self._status.state != PluginState.VALIDATING:
            logger.warning(
                "Native plugin %s is %s: %s",
                HIKVISION_PLUGIN_ID,
                self._status.state,
                self._status.error,
            )
            return

        logger.info(
            "Validating native plugin %s (%s)",
            HIKVISION_PLUGIN_ID,
            self._status.architecture,
        )
        result = await self._run_probe(self._plugins_dir / HIKVISION_PLUGIN_ID)
        if result.succeeded:
            self._status = PluginStatus(
                id=HIKVISION_PLUGIN_ID,
                name=HIKVISION_PLUGIN_NAME,
                kind="native-sdk",
                state=PluginState.READY,
                version=result.version,
                architecture=self._status.architecture,
            )
            logger.info(
                "Native plugin %s is ready (SDK %s, %s)",
                HIKVISION_PLUGIN_ID,
                result.version,
                self._status.architecture,
            )
            return

        self._status = PluginStatus(
            id=HIKVISION_PLUGIN_ID,
            name=HIKVISION_PLUGIN_NAME,
            kind="native-sdk",
            state=PluginState.FAILED,
            architecture=self._status.architecture,
            error=result.error or "Native SDK validation failed.",
        )
        logger.warning("Native plugin %s failed validation: %s", HIKVISION_PLUGIN_ID, result.error)

    async def stop(self) -> None:
        process = self._process
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        self._process = None

    async def _run_probe(self, plugin_path: Path) -> ProbeResult:
        command = list(self._probe_command(plugin_path))
        environment = os.environ.copy()
        library_paths = [str(plugin_path), str(plugin_path / "HCNetSDKCom")]
        if environment.get("LD_LIBRARY_PATH"):
            library_paths.append(environment["LD_LIBRARY_PATH"])
        environment["LD_LIBRARY_PATH"] = os.pathsep.join(library_paths)

        try:
            self._process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
            )
        except (OSError, ValueError) as exc:
            detail = self._sanitize_error(str(exc), plugin_path)
            return ProbeResult(error=f"Could not start the validation process: {detail}.")

        try:
            stdout, stderr = await asyncio.wait_for(
                self._process.communicate(), timeout=self._probe_timeout
            )
        except TimeoutError:
            self._process.kill()
            await self._process.wait()
            return ProbeResult(error="Native SDK validation timed out.")
        except asyncio.CancelledError:
            if self._process.returncode is None:
                self._process.kill()
                await self._process.wait()
            raise
        finally:
            process = self._process
            self._process = None

        output = stdout.decode("utf-8", errors="replace")
        payload = None
        for line in output.splitlines():
            if line.startswith(PROBE_RESULT_PREFIX):
                payload = line.removeprefix(PROBE_RESULT_PREFIX)

        return_code = process.returncode if process is not None else None
        if payload is not None:
            try:
                result = json.loads(payload)
            except json.JSONDecodeError:
                result = None
            if isinstance(result, dict):
                if (
                    return_code == 0
                    and result.get("ok") is True
                    and _VERSION_PATTERN.fullmatch(str(result.get("version", "")))
                ):
                    return ProbeResult(version=str(result["version"]))
                if result.get("ok") is False and isinstance(result.get("error"), str):
                    return ProbeResult(error=self._sanitize_error(result["error"], plugin_path))

        if return_code:
            detail = self._sanitize_error(stderr.decode("utf-8", errors="replace"), plugin_path)
            message = f"Native SDK validation exited unexpectedly (code {return_code})."
            if detail:
                message = f"{message} {detail}"
            return ProbeResult(error=message)
        return ProbeResult(error="Native SDK validation returned an invalid result.")

    @staticmethod
    def _sanitize_error(message: str, plugin_path: Path) -> str:
        concise = " ".join(message.replace(str(plugin_path), "<plugin-dir>").split())
        return concise[:300]
