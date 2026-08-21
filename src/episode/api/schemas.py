from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from episode.api.inventory import DeviceConfigurationResponse, IntegrationSupportResponse
from episode.domain.models import EpisodeState, EventState, ReceiptStatus

OperationalState = Literal["healthy", "degraded", "unavailable", "disabled", "unknown"]


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class HealthResponse(ApiModel):
    status: Literal["ok"]
    version: str


class ApiErrorDetail(ApiModel):
    location: list[str | int] = Field(default_factory=list)
    message: str
    type: str


class ApiError(ApiModel):
    code: str
    message: str
    details: list[ApiErrorDetail] = Field(default_factory=list)


class ApiErrorResponse(ApiModel):
    error: ApiError


class AreaResponse(ApiModel):
    id: str
    name: str
    location: str
    enabled: bool = True
    device_count: int = 0


class IntegrationResponse(ApiModel):
    id: str
    name: str
    type: str
    kind: Literal["device", "shared", "plugin"]
    state: OperationalState
    device_id: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    summary: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class DeviceIdentityResponse(ApiModel):
    manufacturer: str | None = None
    model: str | None = None
    firmware_version: str | None = None


class CapturePolicyResponse(ApiModel):
    recording: str
    automatic_snapshots: bool
    onvif_events: bool | None = None
    activity_window_seconds: int


class DeviceSummaryResponse(ApiModel):
    id: str
    name: str
    device_type: str
    area_id: str
    capabilities: list[str]
    state: OperationalState
    identity: DeviceIdentityResponse
    enabled: bool
    integrations: list[IntegrationResponse] = Field(default_factory=list)


class DeviceDetailResponse(DeviceSummaryResponse):
    ip_address: str
    capture_policy: CapturePolicyResponse
    configuration: DeviceConfigurationResponse
    integration_support: dict[str, IntegrationSupportResponse] = Field(default_factory=dict)
    can_delete: bool = False


class ServiceResponse(ApiModel):
    id: str
    name: str
    state: OperationalState
    summary: str
    metrics: dict[str, Any] = Field(default_factory=dict)


class IntegrationCountsResponse(ApiModel):
    total: int = 0
    healthy: int = 0
    degraded: int = 0
    unavailable: int = 0


class SystemStatusResponse(ApiModel):
    version: str
    state: OperationalState
    active_recordings: int = 0
    services: dict[str, OperationalState]
    integrations: IntegrationCountsResponse


class StorageResponse(ApiModel):
    data_bytes: int = 0
    filesystem_total_bytes: int | None = None
    filesystem_free_bytes: int | None = None


class DiagnosticsResponse(ApiModel):
    status: SystemStatusResponse
    services: list[ServiceResponse]
    integrations: list[IntegrationResponse]
    storage: StorageResponse = Field(default_factory=StorageResponse)


class DiagnosticsExportResponse(ApiModel):
    schema_version: Literal[1] = 1
    generated_at: datetime
    diagnostics: DiagnosticsResponse


class EpisodeResponse(ApiModel):
    id: str
    primary_area_id: str
    start_time: datetime
    last_event_time: datetime | None
    last_activity_at: datetime | None
    minimum_end_at: datetime | None
    end_time: datetime | None
    state: EpisodeState
    event_count: int
    evidence_count: int
    summary: str
    trigger_type: str | None = None


class CurrentViewResponse(ApiModel):
    device_id: str
    device_name: str
    mode: Literal["snapshot", "unavailable"]
    refresh_interval_seconds: int
    image_url: str | None = None
    summary: str


class EventOriginResponse(ApiModel):
    kind: Literal["plugin", "connector", "core", "external", "unknown"]
    id: str
    name: str
    source: str


class EventResponse(ApiModel):
    id: str
    device_id: str
    area_id: str
    timestamp: datetime
    event_type: str
    event_state: EventState | str
    sources: list[str] = Field(default_factory=list)
    origins: list[EventOriginResponse] = Field(default_factory=list)
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
