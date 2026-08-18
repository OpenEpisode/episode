"""Versioned public contract for third-party Episode plugins.

Plugins should import only from this module. Everything below
``episode.plugins`` remains an internal implementation detail.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

PLUGIN_API_VERSION = "1"


def _mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType(dict(value))


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")


class PluginState(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"


class InstanceState(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    FAILED = "failed"
    STOPPED = "stopped"


class ReceiptStatus(StrEnum):
    ACCEPTED = "accepted"
    IGNORED = "ignored"
    REJECTED = "rejected"
    UNMATCHED = "unmatched"


class EventState(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class PluginConfigurationError(ValueError):
    """A safe, user-facing plugin configuration error."""


@dataclass(frozen=True)
class DeviceConfig:
    """Read-only configuration for a Device explicitly assigned to a plugin."""

    id: str
    name: str
    device_type: str
    area_id: str
    address: str = ""
    username: str = ""
    password: str = ""
    configuration: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "configuration", _mapping(self.configuration))


@dataclass(frozen=True)
class RawDelivery:
    """Opaque bytes submitted to Episode's core-owned preservation boundary."""

    device_id: str
    received_at: datetime
    payload: bytes
    source: str = ""
    media_type: str = "application/octet-stream"
    artifact_type: str = "plugin_notification"
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.device_id:
            raise ValueError("RawDelivery device_id is required")
        _require_aware(self.received_at, "RawDelivery received_at")
        object.__setattr__(self, "payload", bytes(self.payload))
        object.__setattr__(self, "metadata", _mapping(self.metadata))


@dataclass(frozen=True)
class StoredDelivery:
    """A sealed delivery exposed to a plugin handler after preservation."""

    receipt_id: str
    artifact_id: str
    received_at: datetime
    payload: bytes
    media_type: str
    byte_size: int
    sha256: str
    device_id: str
    area_id: str
    source: str
    transport: str
    original_filename: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_aware(self.received_at, "StoredDelivery received_at")
        object.__setattr__(self, "payload", bytes(self.payload))
        object.__setattr__(self, "metadata", _mapping(self.metadata))


@dataclass(frozen=True)
class EventObservation:
    timestamp: datetime
    event_type: str
    event_state: EventState | str = EventState.ACTIVE
    source: str = ""
    dedup_key: str = ""
    device_id: str = ""
    device_address: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_aware(self.timestamp, "EventObservation timestamp")
        if not self.event_type:
            raise ValueError("EventObservation event_type is required")
        object.__setattr__(self, "event_state", EventState(self.event_state))
        object.__setattr__(self, "metadata", _mapping(self.metadata))


@dataclass(frozen=True)
class EvidenceObservation:
    timestamp: datetime
    evidence_type: str
    mime_type: str
    source: str = ""
    original_filename: str | None = None
    device_id: str = ""
    device_address: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_aware(self.timestamp, "EvidenceObservation timestamp")
        if not self.evidence_type or not self.mime_type:
            raise ValueError("EvidenceObservation type and mime_type are required")
        object.__setattr__(self, "metadata", _mapping(self.metadata))


@dataclass(frozen=True)
class HandlerResult:
    claimed: bool = False
    status: ReceiptStatus | str = ReceiptStatus.ACCEPTED
    event: EventObservation | None = None
    evidence: EvidenceObservation | None = None
    external_id: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.event is not None and self.evidence is not None:
            raise ValueError("A handler result cannot contain both an Event and Evidence")
        if not self.claimed and (self.event is not None or self.evidence is not None):
            raise ValueError("A handler must claim a delivery before returning an observation")
        object.__setattr__(self, "status", ReceiptStatus(self.status))
        object.__setattr__(self, "metadata", _mapping(self.metadata))


DeliveryMatcher = Callable[[StoredDelivery], bool]
DeliveryHandler = Callable[[StoredDelivery], Awaitable[HandlerResult]]


@dataclass(frozen=True)
class HandlerRegistration:
    id: str
    matcher: DeliveryMatcher
    handler: DeliveryHandler
    timeout: float = 5.0

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("Handler id is required")
        if self.timeout <= 0:
            raise ValueError("Handler timeout must be greater than zero")


class Ingress(Protocol):
    """Raw-first ingress capabilities granted to one configured plugin."""

    def register(self, registration: HandlerRegistration) -> None: ...

    def unregister(self, handler_id: str) -> None: ...

    async def submit(self, delivery: RawDelivery) -> None: ...

    def status(self, handler_id: str) -> Mapping[str, object] | None: ...


@dataclass(frozen=True)
class MediaSource:
    """A runtime video stream and optional snapshot endpoint for one Device."""

    device_id: str
    stream_uri: str = ""
    snapshot_uri: str = ""
    username: str = ""
    password: str = ""
    profile_token: str = ""
    source: str = ""

    def __post_init__(self) -> None:
        if not self.device_id:
            raise ValueError("MediaSource device_id is required")
        if not self.stream_uri and not self.snapshot_uri:
            raise ValueError("MediaSource requires a stream_uri or snapshot_uri")


class Media(Protocol):
    """Scoped runtime media registration for assigned Devices."""

    def register(self, source: MediaSource) -> None: ...

    def unregister(self, device_id: str) -> None: ...


@dataclass(frozen=True)
class InstanceStatus:
    id: str
    name: str
    state: InstanceState
    messages_received: int = 0
    connected_at: datetime | None = None
    last_message_at: datetime | None = None
    error: str | None = None
    summary: str | None = None
    capabilities: tuple[str, ...] = ()
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", _mapping(self.details))


@dataclass(frozen=True)
class PluginStatus:
    state: PluginState
    error: str | None = None
    summary: str | None = None
    instances: tuple[InstanceStatus, ...] = ()
    metrics: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metrics", _mapping(self.metrics))


@dataclass(frozen=True)
class PluginContext:
    """Capabilities and scoped configuration supplied to one plugin instance."""

    plugin_id: str
    plugin_dir: Path
    settings: Mapping[str, object]
    devices: tuple[DeviceConfig, ...]
    ingress: Ingress
    media: Media

    def __post_init__(self) -> None:
        object.__setattr__(self, "plugin_dir", Path(self.plugin_dir))
        object.__setattr__(self, "settings", _mapping(self.settings))


class Plugin(Protocol):
    def status(self) -> PluginStatus: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


PluginFactory = Callable[[PluginContext], Plugin]


__all__ = [
    "PLUGIN_API_VERSION",
    "DeviceConfig",
    "EventObservation",
    "EventState",
    "EvidenceObservation",
    "HandlerRegistration",
    "HandlerResult",
    "Ingress",
    "InstanceState",
    "InstanceStatus",
    "Media",
    "MediaSource",
    "Plugin",
    "PluginConfigurationError",
    "PluginContext",
    "PluginFactory",
    "PluginState",
    "PluginStatus",
    "RawDelivery",
    "ReceiptStatus",
    "StoredDelivery",
]
