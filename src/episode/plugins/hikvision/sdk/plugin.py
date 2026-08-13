from __future__ import annotations

import asyncio
import logging
import os
import platform
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Callable, Sequence

from episode.domain.models import ReceiptStatus
from episode.ingestion.models import (
    EventObservation,
    IngressHandlerResult,
    StoredIngressEnvelope,
)
from episode.ingestion.router import IngressHandlerRegistration, IngressRouter
from episode.plugins.hikvision.sdk.events import interpret_event
from episode.plugins.hikvision.sdk.runtime import SDKDeviceConfig, SDKDeviceWorker
from episode.plugins.models import (
    PluginContext,
    PluginInstanceState,
    PluginInstanceStatus,
    PluginState,
    PluginStatus,
    RawPluginDeliverySink,
)
from episode.plugins.probe import SubprocessProbeRunner

logger = logging.getLogger(__name__)

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
WorkerFactory = Callable[[Path, SDKDeviceConfig, RawPluginDeliverySink], SDKDeviceWorker]


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
        "episode.plugins.hikvision.sdk.probe",
        str(plugin_path),
    ]


def _default_worker_factory(
    plugin_path: Path,
    config: SDKDeviceConfig,
    sink: RawPluginDeliverySink,
) -> SDKDeviceWorker:
    return SDKDeviceWorker(plugin_path, config, sink)


def _configured_sdk_devices(
    devices: tuple[Mapping[str, object], ...],
) -> list[Mapping[str, object]]:
    selected = []
    for device in devices:
        capabilities = device.get("capabilities", [])
        if isinstance(capabilities, (list, tuple, set)) and "hikvision_sdk" in capabilities:
            selected.append(device)
    return selected


def _device_config(device: Mapping[str, object]) -> tuple[SDKDeviceConfig | None, str | None]:
    device_id = device.get("id")
    name = device.get("name")
    area_id = device.get("area_id")
    address = device.get("ip_address")
    username = device.get("username")
    password = device.get("password")
    configs = device.get("configs", {})
    sdk_config = configs.get("hikvision_sdk", {}) if isinstance(configs, Mapping) else {}
    settings = sdk_config.get("settings", {}) if isinstance(sdk_config, Mapping) else {}
    port = sdk_config.get("port") if isinstance(sdk_config, Mapping) else None
    if port is None and isinstance(settings, Mapping):
        port = settings.get("port")
    if port is None:
        port = 8000

    display_id = device_id if isinstance(device_id, str) and device_id else "unknown-device"
    required = {
        "id": device_id,
        "name": name,
        "area_id": area_id,
        "ip_address": address,
        "username": username,
        "password": password,
    }
    missing = [key for key, value in required.items() if not isinstance(value, str) or not value]
    if missing:
        return None, f"Missing SDK device configuration: {', '.join(missing)}."
    if not isinstance(port, int) or not 1 <= port <= 65535:
        return None, "The SDK port must be between 1 and 65535."

    return (
        SDKDeviceConfig(
            id=display_id,
            name=name,
            area_id=area_id,
            address=address,
            port=port,
            username=username,
            password=password,
        ),
        None,
    )


class HikvisionSDKPlugin:
    def __init__(
        self,
        context: PluginContext,
        *,
        runner: SubprocessProbeRunner | None = None,
        probe_command: ProbeCommand | None = None,
        worker_factory: WorkerFactory | None = None,
        host_machine: str | None = None,
    ):
        self._path = context.plugins_dir / PLUGIN_ID
        self._configured_devices = _configured_sdk_devices(context.configured_devices)
        self._delivery_sink = context.raw_delivery_sink
        self._ingress_router = (
            context.ingress_router if isinstance(context.ingress_router, IngressRouter) else None
        )
        self._handler_registered = False
        self._runner = runner or SubprocessProbeRunner()
        self._probe_command = probe_command or _default_probe_command
        self._worker_factory = worker_factory or _default_worker_factory
        self._host_machine = host_machine
        self._workers: list[SDKDeviceWorker] = []
        self._invalid_instances: list[PluginInstanceStatus] = []
        self._status = PluginStatus(
            id=PLUGIN_ID,
            name=PLUGIN_NAME,
            kind=PLUGIN_KIND,
            state=PluginState.VALIDATING,
        )

    @staticmethod
    def _matches_ingress(envelope: StoredIngressEnvelope) -> bool:
        return envelope.transport == "plugin" and envelope.metadata.get("plugin_id") == PLUGIN_ID

    @staticmethod
    async def _interpret_ingress(envelope: StoredIngressEnvelope) -> IngressHandlerResult:
        command = envelope.metadata.get("command")
        if not isinstance(command, int):
            return IngressHandlerResult(
                claimed=True,
                status=ReceiptStatus.REJECTED,
                metadata={"reason": "missing_sdk_command"},
            )

        event = interpret_event(
            command,
            envelope.payload,
            envelope.device_id,
            envelope.received_at,
        )
        if event is None:
            return IngressHandlerResult(
                claimed=True,
                metadata={"interpreted": False, "sdk_command": command},
            )
        return IngressHandlerResult(
            claimed=True,
            event=EventObservation(
                timestamp=event.timestamp,
                event_type=event.event_type,
                event_state=event.event_state,
                source=event.source,
                device_id=envelope.device_id,
                area_id=envelope.area_id,
                dedup_key=event.dedup_key,
                metadata=event.metadata,
            ),
            metadata={"interpreted": True, "sdk_command": command},
        )

    def status(self) -> PluginStatus:
        metrics = (
            self._ingress_router.status("hikvision-sdk-events")
            if self._ingress_router is not None
            else None
        )
        instances = (
            *self._invalid_instances,
            *(worker.status() for worker in self._workers),
        )
        if not instances or self._status.state != PluginState.READY:
            return PluginStatus(
                **{
                    **self._status.public(),
                    "instances": tuple(instances),
                    "metrics": metrics or {},
                }
            )

        running = sum(instance.state == PluginInstanceState.RUNNING for instance in instances)
        if running == len(instances):
            state = PluginState.READY
            error = None
        elif running:
            state = PluginState.DEGRADED
            error = f"{len(instances) - running} SDK device worker(s) unavailable."
        else:
            state = PluginState.FAILED
            error = "No configured SDK device workers are available."
        return PluginStatus(
            id=self._status.id,
            name=self._status.name,
            kind=self._status.kind,
            state=state,
            version=self._status.version,
            architecture=self._status.architecture,
            error=error,
            instances=tuple(instances),
            metrics=metrics or {},
        )

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
        if not (
            result.succeeded and isinstance(version, str) and _VERSION_PATTERN.fullmatch(version)
        ):
            error = result.error or "HCNetSDK validation returned an invalid version."
            self._status = PluginStatus(
                id=PLUGIN_ID,
                name=PLUGIN_NAME,
                kind=PLUGIN_KIND,
                state=PluginState.FAILED,
                architecture=self._status.architecture,
                error=error,
            )
            return

        self._status = PluginStatus(
            id=PLUGIN_ID,
            name=PLUGIN_NAME,
            kind=PLUGIN_KIND,
            state=PluginState.READY,
            version=version,
            architecture=self._status.architecture,
        )
        if self._ingress_router is not None:
            self._ingress_router.register(
                IngressHandlerRegistration(
                    id="hikvision-sdk-events",
                    matcher=self._matches_ingress,
                    handler=self._interpret_ingress,
                )
            )
            self._handler_registered = True
        if not self._configured_devices:
            return
        if self._delivery_sink is None:
            self._invalid_instances = [
                PluginInstanceStatus(
                    id=str(device.get("id") or "unknown-device"),
                    name=str(device.get("name") or device.get("id") or "Unknown device"),
                    state=PluginInstanceState.FAILED,
                    error="Raw plugin delivery storage is unavailable.",
                )
                for device in self._configured_devices
            ]
            return

        for device in self._configured_devices:
            config, error = _device_config(device)
            if config is None:
                device_id = str(device.get("id") or "unknown-device")
                self._invalid_instances.append(
                    PluginInstanceStatus(
                        id=device_id,
                        name=str(device.get("name") or device_id),
                        state=PluginInstanceState.FAILED,
                        error=error,
                    )
                )
                continue
            self._workers.append(self._worker_factory(self._path, config, self._delivery_sink))

        results = await asyncio.gather(
            *(worker.start() for worker in self._workers),
            return_exceptions=True,
        )
        for worker, result in zip(self._workers, results, strict=True):
            if isinstance(result, BaseException):
                logger.error(
                    "HCNetSDK worker for device %s failed during startup",
                    worker.status().id,
                    exc_info=result,
                )

    async def stop(self) -> None:
        await asyncio.gather(
            *(worker.stop() for worker in reversed(self._workers)),
            return_exceptions=True,
        )
        self._workers.clear()
        if self._handler_registered and self._ingress_router is not None:
            self._ingress_router.unregister("hikvision-sdk-events")
            self._handler_registered = False
        await self._runner.stop()
