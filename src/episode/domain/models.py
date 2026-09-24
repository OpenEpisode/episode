from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from urllib.parse import quote
from uuid import uuid4

from episode.domain.event_filter import normalize_device_selector, normalize_selector


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
        # An IPv6 address is a single URL host only when enclosed in brackets.
        address = f"[{host}]" if ":" in host and not host.startswith("[") else host
        auth = f"{quote(username, safe='')}:{quote(password, safe='')}@" if username else ""
        port_str = f":{self.port}" if self.port else ""
        return f"{self.protocol}://{auth}{address}{port_str}{self.path}"


class EventState(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class EpisodeState(str, Enum):
    NEW = "new"
    ACTIVE = "active"
    QUIESCENT = "quiescent"
    FINALIZING = "finalizing"
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

    ``filtered_event_type`` records the normalized event type suppressed by the
    event filter (when ``reason == "generic_event_filtered"``) so the exact
    suppressed observation is traceable. ``filtered_event_class`` records which
    class caused suppression and ``filter_source`` which level decided, so the
    decision stays explainable without re-reading configuration. ``attachment``
    records what happened instead of driving capture: the Event was linked to an
    already-open Episode, or there was none to link to.
    """

    allowed: bool
    profile_id: str
    profile_name: str
    reason: str
    evaluated_at: datetime
    filtered_event_type: str | None = None
    filtered_event_class: str | None = None
    filter_source: str | None = None
    attachment: str | None = None
    # Snapshot of the Device's effective activity window at canonicalization.
    # This lets crash recovery rebuild an Episode without reinterpreting later
    # inventory edits. Older persisted decisions omit the value.
    activity_window_seconds: int | None = None


@dataclass
class CaptureProfile:
    """A named set of Devices eligible to participate in new capture.

    ``event_filter`` lists the event classes this profile suppresses for its
    Devices. Every class, including ``security`` and ``access``, is selectable
    here and at the per-Device level. A per-Device ``event_filter`` override
    takes priority over this profile default, and an empty list means no
    filtering.
    """

    id: str = ""
    name: str = ""
    include_all_devices: bool = False
    device_ids: list[str] = field(default_factory=list)
    builtin: bool = False
    active: bool = False
    event_filter: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))

    def __post_init__(self):
        # A profile selector is always a concrete set; "inherit" is not a valid
        # profile state. Protected or malformed entries normalize away so a
        # damaged stored value means "no filtering".
        self.event_filter = sorted(normalize_selector(self.event_filter))


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
    # Event-class filter override. ``None`` means inherit the active Capture
    # Profile; a list selects classes for this Device alone, and an empty list
    # is the explicit negative "this camera filters nothing".
    event_filter: list[str] | None = None
    setup_state: str = "ready"

    def __post_init__(self):
        if self.setup_state not in {"ready", "needs_setup"}:
            raise ValueError("Device setup state must be ready or needs_setup")
        # ``None`` stays inherit. An unreadable selector also means inherit, so a
        # corrupt row cannot silently opt a camera out of profile policy; operator
        # input is validated at the API edge.
        self.event_filter = normalize_device_selector(self.event_filter)
        if self.activity_window_seconds is not None and self.activity_window_seconds < 1:
            raise ValueError("Device activity window must be positive")
        if self.configs and isinstance(next(iter(self.configs.values()), None), dict):
            self.configs = {
                k: CapabilityConfig(**v) if isinstance(v, dict) else v
                for k, v in self.configs.items()
            }

    def get_config(self, capability: str) -> CapabilityConfig | None:
        return self.configs.get(capability)

    @property
    def can_participate(self) -> bool:
        """Whether this Device may contribute to new capture work."""
        return self.enabled and self.setup_state == "ready"


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
