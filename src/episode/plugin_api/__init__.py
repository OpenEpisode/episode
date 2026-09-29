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
class VideoMode:
    """A configured or camera-advertised encoding mode for a media source."""

    width: int | None = None
    height: int | None = None
    frame_rates: tuple[int, ...] = ()
    codec: str = ""

    def __post_init__(self) -> None:
        for name, value in (("width", self.width), ("height", self.height)):
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 16384
            ):
                raise ValueError(f"VideoMode {name} must be between 1 and 16384")
        if not isinstance(self.frame_rates, (tuple, list)):
            raise ValueError("VideoMode frame_rates must be a tuple or list")
        frame_rates = tuple(self.frame_rates)
        if len(frame_rates) > 32 or any(
            not isinstance(rate, int) or isinstance(rate, bool) or not 1 <= rate <= 240
            for rate in frame_rates
        ):
            raise ValueError("VideoMode frame rates must be between 1 and 240 fps")
        object.__setattr__(self, "frame_rates", frame_rates)
        if (
            not isinstance(self.codec, str)
            or len(self.codec) > 32
            or any(ord(character) < 32 for character in self.codec)
            or "://" in self.codec
        ):
            raise ValueError("VideoMode codec is invalid or too long")


@dataclass(frozen=True)
class VideoSourceInfo:
    """Credential-free, selectable source metadata reported during discovery.

    ``metadata_kind`` distinguishes active configuration and observed media from
    a device's advertised capabilities, which may not match its current settings.
    """

    id: str
    name: str
    protocol: str = "unknown"
    metadata_kind: str = "unknown"
    width: int | None = None
    height: int | None = None
    frame_rate: float | None = None
    codec: str = ""
    modes: tuple[VideoMode, ...] = ()
    default: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id or len(self.id) > 256:
            raise ValueError("VideoSourceInfo id must contain 1 to 256 characters")
        if any(ord(character) < 32 for character in self.id) or "://" in self.id:
            raise ValueError("VideoSourceInfo id cannot contain control characters or a URL")
        if not isinstance(self.name, str) or not self.name or len(self.name) > 120:
            raise ValueError("VideoSourceInfo name must contain 1 to 120 characters")
        if not isinstance(self.protocol, str) or len(self.protocol) > 32:
            raise ValueError("VideoSourceInfo protocol is too long")
        if not isinstance(self.codec, str) or len(self.codec) > 32:
            raise ValueError("VideoSourceInfo protocol or codec is too long")
        for value in (self.name, self.protocol, self.codec):
            if any(ord(character) < 32 for character in value) or "://" in value:
                raise ValueError(
                    "VideoSourceInfo labels cannot contain control characters or a URL"
                )
        if self.metadata_kind not in {"configured", "capabilities", "observed", "unknown"}:
            raise ValueError("VideoSourceInfo metadata_kind is unsupported")
        for name, value in (("width", self.width), ("height", self.height)):
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 16384
            ):
                raise ValueError(f"VideoSourceInfo {name} must be between 1 and 16384")
        if not isinstance(self.modes, (tuple, list)):
            raise ValueError("VideoSourceInfo modes must be a tuple or list")
        modes = tuple(self.modes)
        if len(modes) > 32 or any(not isinstance(mode, VideoMode) for mode in modes):
            raise ValueError("VideoSourceInfo has too many encoding modes")
        object.__setattr__(self, "modes", modes)
        if self.frame_rate is not None and (
            isinstance(self.frame_rate, bool)
            or not isinstance(self.frame_rate, (int, float))
            or not 0 < self.frame_rate <= 240
        ):
            raise ValueError("VideoSourceInfo frame_rate must be between 0 and 240 fps")
        if not isinstance(self.default, bool):
            raise ValueError("VideoSourceInfo default must be a boolean")


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
    video_source: VideoSourceInfo | None = None

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
    "VideoMode",
    "VideoSourceInfo",
]
