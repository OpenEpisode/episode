from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType

from episode.domain.models import ReceiptStatus


def _immutable_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType(dict(value))


@dataclass(frozen=True)
class IngressDelivery:
    source: str
    transport: str
    received_at: datetime
    payload: bytes
    media_type: str = "application/octet-stream"
    artifact_type: str = "event_payload"
    device_id: str = ""
    area_id: str = ""
    original_filename: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", bytes(self.payload))
        object.__setattr__(self, "metadata", _immutable_mapping(self.metadata))


@dataclass(frozen=True)
class StoredIngressEnvelope:
    receipt_id: str
    artifact_id: str
    source: str
    transport: str
    received_at: datetime
    payload: bytes
    media_type: str
    byte_size: int = 0
    sha256: str = ""
    sealed: bool = False
    device_id: str = ""
    area_id: str = ""
    original_filename: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", bytes(self.payload))
        object.__setattr__(self, "metadata", _immutable_mapping(self.metadata))


@dataclass(frozen=True)
class EventObservation:
    timestamp: datetime
    event_type: str
    event_state: str
    source: str
    device_id: str = ""
    area_id: str = ""
    device_ip: str = ""
    dedup_key: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _immutable_mapping(self.metadata))


@dataclass(frozen=True)
class IngressHandlerResult:
    claimed: bool = False
    status: ReceiptStatus = ReceiptStatus.ACCEPTED
    event: EventObservation | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _immutable_mapping(self.metadata))
