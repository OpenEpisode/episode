from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from episode.domain.models import EpisodeState, EventState, ReceiptStatus


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class AreaResponse(ApiModel):
    id: str
    name: str
    location: str
    enabled: bool = True
    device_count: int = 0


class EpisodeResponse(ApiModel):
    id: str
    primary_area_id: str
    start_time: datetime
    last_event_time: datetime | None
    last_activity_at: datetime | None
    end_time: datetime | None
    state: EpisodeState
    event_count: int
    evidence_count: int
    summary: str
    trigger_type: str | None = None


class EventResponse(ApiModel):
    id: str
    device_id: str
    area_id: str
    timestamp: datetime
    event_type: str
    event_state: EventState | str
    sources: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    episode_id: str | None
    has_raw_payload: bool = False


class EvidenceResponse(ApiModel):
    id: str
    device_id: str
    area_id: str
    timestamp: datetime
    evidence_type: str
    mime_type: str
    original_filename: str | None
    artifact_id: str | None
    byte_size: int | None
    sha256: str | None
    metadata: dict[str, Any] = Field(default_factory=dict)
    event_id: str | None
    episode_id: str | None


class IngestionReceiptResponse(ApiModel):
    id: str
    source: str
    received_at: datetime
    observed_at: datetime | None
    status: ReceiptStatus
    artifact_id: str | None
    device_id: str
    area_id: str
    external_id: str | None
    metadata: dict[str, Any] = Field(default_factory=dict)
    event_id: str | None
    evidence_id: str | None
    episode_id: str | None
    has_artifact: bool = False
    transport: str | None = None
    reason: str | None = None


class ClosestSnapshotResponse(ApiModel):
    snapshot: EvidenceResponse
    bounding_box: dict[str, float] | None
    target_type: str | None


class ClosestEventResponse(ApiModel):
    event: EventResponse
    bounding_box: dict[str, float] | None
    target_type: str | None
