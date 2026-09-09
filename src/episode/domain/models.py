from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from uuid import uuid4


def make_episode_id(timestamp: datetime | None = None) -> str:
    if timestamp is None:
        timestamp = datetime.now(tz=timezone.utc)
    ts = timestamp.strftime("%Y%m%d_%H%M%S")
    suffix = str(uuid4())[:8]
    return f"{ts}_{suffix}"


def make_event_dedup_key(
    device_id: str,
    timestamp: datetime,
    event_type: str,
    event_state: EventState | str,
) -> str:
    """Return the stable identity of one observation across connector deliveries."""
    state = event_state.value if isinstance(event_state, EventState) else event_state
    observed_at = timestamp.astimezone(timezone.utc) if timestamp.tzinfo else timestamp
    value = "\x1f".join((device_id, observed_at.isoformat(), event_type, state))
    return sha256(value.encode()).hexdigest()


@dataclass
class CapabilityConfig:
    protocol: str = ""
    port: int | None = None
    path: str = ""
    settings: dict = field(default_factory=dict)

    def build_url(self, host: str, username: str = "", password: str = "") -> str:
        if not self.protocol or not host:
            return ""
        auth = f"{username}:{password}@" if username else ""
        port_str = f":{self.port}" if self.port else ""
        return f"{self.protocol}://{auth}{host}{port_str}{self.path}"


class EventState(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class EpisodeState(str, Enum):
    NEW = "new"
    ACTIVE = "active"
    QUIESCENT = "quiescent"
    CLOSED = "closed"
    ARCHIVED = "archived"


class ReceiptStatus(str, Enum):
    ACCEPTED = "accepted"
    IGNORED = "ignored"
    REJECTED = "rejected"
    UNMATCHED = "unmatched"


@dataclass(frozen=True)
class ParticipationDecision:
    """The core-owned capture decision persisted with a canonical Event.

    Participation is intentionally separate from integration metadata.  It is
    an operational interpretation of whether an active observation may affect
    Episode capture, and remains useful after the active profile changes.
    """

    allowed: bool
    profile_id: str
    profile_name: str
    reason: str
    evaluated_at: datetime


@dataclass
class CaptureProfile:
    """A named set of Devices eligible to participate in new capture."""

    id: str = ""
    name: str = ""
    include_all_devices: bool = False
    device_ids: list[str] = field(default_factory=list)
    builtin: bool = False
    active: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))


@dataclass(frozen=True)
class CaptureProfileChange:
    """One append-only active-profile transition."""

    previous_profile_id: str | None
    previous_profile_name: str | None
    new_profile_id: str
    new_profile_name: str
    changed_at: datetime
    source: str


@dataclass
class Area:
    id: str = ""
    name: str = ""
    location: str = ""
    metadata: dict = field(default_factory=dict)
    enabled: bool = True


@dataclass
class Device:
    id: str = ""
    name: str = ""
    device_type: str = ""
    area_id: str = ""
    capabilities: list[str] = field(default_factory=list)
    ip_address: str = ""
    username: str = ""
    password: str = ""
    configs: dict[str, CapabilityConfig] = field(default_factory=dict)
    activity_window_seconds: int | None = None
    metadata: dict = field(default_factory=dict)
    enabled: bool = True

    def __post_init__(self):
        if self.activity_window_seconds is not None and self.activity_window_seconds < 1:
            raise ValueError("Device activity window must be positive")
        if self.configs and isinstance(next(iter(self.configs.values()), None), dict):
            self.configs = {
                k: CapabilityConfig(**v) if isinstance(v, dict) else v
                for k, v in self.configs.items()
            }

    def get_config(self, capability: str) -> CapabilityConfig | None:
        return self.configs.get(capability)


@dataclass
class Event:
    id: str = field(default_factory=lambda: str(uuid4()))
    device_id: str = ""
    area_id: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    event_type: str = ""
    event_state: EventState | str = EventState.ACTIVE
    source: str = ""
    dedup_key: str = ""
    raw_payload_path: str | None = None
    metadata: dict = field(default_factory=dict)
    episode_id: str | None = None
    participation: ParticipationDecision | None = None
    # ``None`` means that no participation snapshot exists. An empty list is
    # meaningful: the Event had no eligible recording targets, either because
    # capture was excluded or no video target matched.
    eligible_recording_device_ids: list[str] | None = None

    def __post_init__(self):
        if isinstance(self.event_state, str):
            self.event_state = EventState(self.event_state)
        if isinstance(self.participation, dict):
            decision = dict(self.participation)
            evaluated_at = decision.get("evaluated_at")
            if isinstance(evaluated_at, str):
                decision["evaluated_at"] = datetime.fromisoformat(evaluated_at)
            self.participation = ParticipationDecision(**decision)
        if self.eligible_recording_device_ids is not None:
            self.eligible_recording_device_ids = list(self.eligible_recording_device_ids)


@dataclass
class Evidence:
    id: str = field(default_factory=lambda: str(uuid4()))
    device_id: str = ""
    area_id: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    evidence_type: str = ""
    file_path: str = ""
    mime_type: str = ""
    original_filename: str | None = None
    artifact_id: str | None = None
    byte_size: int | None = None
    sha256: str | None = None
    metadata: dict = field(default_factory=dict)
    event_id: str | None = None
    episode_id: str | None = None
    availability: str = "available"
    expired_at: datetime | None = None
    expiration_reason: str | None = None


@dataclass
class RawArtifact:
    id: str = field(default_factory=lambda: str(uuid4()))
    artifact_type: str = ""
    file_path: str = ""
    mime_type: str = "application/octet-stream"
    byte_size: int = 0
    sha256: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    original_filename: str | None = None
    sealed: bool = True
    metadata: dict = field(default_factory=dict)


@dataclass
class IngestionReceipt:
    id: str = field(default_factory=lambda: str(uuid4()))
    source: str = ""
    received_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    observed_at: datetime | None = None
    status: ReceiptStatus | str = ReceiptStatus.ACCEPTED
    artifact_id: str | None = None
    device_id: str = ""
    area_id: str = ""
    external_id: str | None = None
    metadata: dict = field(default_factory=dict)
    event_id: str | None = None
    evidence_id: str | None = None
    episode_id: str | None = None

    def __post_init__(self):
        if isinstance(self.status, str):
            self.status = ReceiptStatus(self.status)


@dataclass
class Episode:
    id: str = field(default_factory=lambda: str(uuid4()))
    primary_area_id: str = ""
    start_time: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    last_event_time: datetime | None = None
    last_activity_at: datetime | None = None
    minimum_end_at: datetime | None = None
    end_time: datetime | None = None
    state: EpisodeState = EpisodeState.NEW
    event_count: int = 0
    evidence_count: int = 0
    summary: str = ""
