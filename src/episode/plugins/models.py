from __future__ import annotations

from collections.abc import Awaitable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Callable, Protocol


class PluginState(StrEnum):
    NOT_INSTALLED = "not_installed"
    INCOMPLETE = "incomplete"
    INCOMPATIBLE = "incompatible"
    VALIDATING = "validating"
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"


class PluginInstanceState(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass(frozen=True)
class PluginDeviceInfo:
    manufacturer: str | None = None
    model: str | None = None
    firmware_version: str | None = None


@dataclass(frozen=True)
class PluginInstanceStatus:
    id: str
    name: str
    state: PluginInstanceState
    messages_received: int = 0
    connected_at: datetime | None = None
    last_message_at: datetime | None = None
    error: str | None = None
    device_info: PluginDeviceInfo | None = None


@dataclass(frozen=True)
class PluginStatus:
    id: str
    name: str
    kind: str
    state: PluginState
    version: str | None = None
    architecture: str | None = None
    error: str | None = None
    instances: tuple[PluginInstanceStatus, ...] = ()

    def public(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class PluginEvent:
    timestamp: datetime
    event_type: str
    event_state: str
    source: str
    dedup_key: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class RawPluginDelivery:
    plugin_id: str
    device_id: str
    area_id: str
    received_at: datetime
    payload: bytes
    metadata: Mapping[str, object]
    event: PluginEvent | None = None


RawPluginDeliverySink = Callable[[RawPluginDelivery], Awaitable[None]]


@dataclass(frozen=True)
class PluginContext:
    plugins_dir: Path
    configured_devices: tuple[Mapping[str, object], ...] = ()
    raw_delivery_sink: RawPluginDeliverySink | None = None


class ManagedPlugin(Protocol):
    def status(self) -> PluginStatus: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


PluginFactory = Callable[[PluginContext], ManagedPlugin]


@dataclass(frozen=True)
class PluginRegistration:
    id: str
    name: str
    kind: str
    activation_capability: str
    factory: PluginFactory

    def validating_status(self) -> PluginStatus:
        return PluginStatus(
            id=self.id,
            name=self.name,
            kind=self.kind,
            state=PluginState.VALIDATING,
        )
