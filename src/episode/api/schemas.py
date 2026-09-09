from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from episode.api.inventory import DeviceConfigurationResponse, IntegrationSupportResponse
from episode.domain.lifecycle import (
    MAX_QUIESCENT_GRACE_SECONDS,
    MIN_QUIESCENT_GRACE_SECONDS,
)
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


class RecordingDiagnosticResponse(ApiModel):
    evidence_id: str
    episode_id: str
    device_id: str
    started_at: datetime
    state: Literal["starting", "recording", "reconnecting", "stalled", "failed"]
    ready: bool = False
    fragment_count: int = 0
    last_fragment_at: datetime | None = None
    reconnect_count: int = 0
    last_exit_code: int | None = None
    last_error: str | None = None


class RecordingIssueResponse(ApiModel):
    evidence_id: str
    episode_id: str | None = None
    device_id: str
    timestamp: datetime
    reason: str | None = None


class RetentionSettingsUpdate(ApiModel):
    enabled: bool
    retention_days: int = Field(ge=1, le=3650)


class RetentionSettingsResponse(ApiModel):
    enabled: bool
    retention_days: int
    policy_state: Literal["unconfirmed", "configured", "disabled"]
    confirmed_at: datetime | None = None
    notice: str
    state: OperationalState
    last_cleanup_at: datetime | None = None
    expired_count: int = 0
    failure_count: int = 0
    last_error: str | None = None


class EpisodeLifecycleSettingsUpdate(ApiModel):
    quiescent_grace_seconds: int = Field(
        ge=MIN_QUIESCENT_GRACE_SECONDS,
        le=MAX_QUIESCENT_GRACE_SECONDS,
    )


class EpisodeLifecycleSettingsResponse(ApiModel):
    quiescent_grace_seconds: int
    default_quiescent_grace_seconds: int
    min_quiescent_grace_seconds: int
    max_quiescent_grace_seconds: int
    notice: str


class InstallationSettingsUpdate(ApiModel):
    external_url: str = Field(default="", max_length=2048)


class InstallationSettingsResponse(ApiModel):
    external_url: str


class EpisodeStartedWebhookSettingsUpdate(ApiModel):
    """Webhook configuration fields.

    ``url`` is accepted here but intentionally has no counterpart on the
    response model. An omitted or blank URL preserves the saved credential;
    ``clear_url`` is the explicit removal operation.
    """

    enabled: bool | None = None
    payload_format: Literal["generic", "discord"] | None = None
    timeout_seconds: float | None = Field(default=None, ge=0.5, le=30)
    url: str | None = Field(
        default=None,
        max_length=2048,
        json_schema_extra={"writeOnly": True},
    )
    clear_url: bool = False


class EpisodeStartedWebhookSettingsResponse(ApiModel):
    enabled: bool
    payload_format: Literal["generic", "discord"]
    timeout_seconds: float
    url_configured: bool


class EpisodeStartedWebhookTestResponse(ApiModel):
    success: bool
    message: str
    status_code: int | None = None


class CaptureProfileCreateRequest(ApiModel):
    name: str = Field(min_length=1, max_length=100)
    device_ids: list[str] = Field(default_factory=list, max_length=500)


class CaptureProfileUpdateRequest(CaptureProfileCreateRequest):
    pass


class CaptureProfileActivationRequest(ApiModel):
    profile_id: str = Field(min_length=1, max_length=128)


class CaptureProfileResponse(ApiModel):
    id: str
    name: str
    include_all_devices: bool
    device_ids: list[str] = Field(default_factory=list)
    builtin: bool
    active: bool


class CaptureProfileChangeResponse(ApiModel):
    previous_profile_id: str | None
    previous_profile_name: str | None
    new_profile_id: str
    new_profile_name: str
    changed_at: datetime
    source: str


class ActiveCaptureProfileResponse(ApiModel):
    profile: CaptureProfileResponse
    recent_changes: list[CaptureProfileChangeResponse] = Field(default_factory=list)


class DiagnosticsResponse(ApiModel):
    status: SystemStatusResponse
    services: list[ServiceResponse]
    integrations: list[IntegrationResponse]
    storage: StorageResponse = Field(default_factory=StorageResponse)
    retention: dict[str, Any] = Field(default_factory=dict)
    recordings: list[RecordingDiagnosticResponse] = Field(default_factory=list)
    recording_issues: list[RecordingIssueResponse] = Field(default_factory=list)


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
    mode: Literal["hls", "snapshot", "unavailable"]
    refresh_interval_seconds: int
    image_url: str | None = None
    stream_url: str | None = None
    recording_state: str | None = None
    fragment_count: int = 0
    last_fragment_at: datetime | None = None
    summary: str


class EventOriginResponse(ApiModel):
    kind: Literal["plugin", "connector", "core", "external", "unknown"]
    id: str
    name: str
    source: str


class EventParticipationResponse(ApiModel):
    allowed: bool
    profile_id: str
    profile_name: str
    reason: str
    evaluated_at: datetime


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
    participation: EventParticipationResponse | None = None


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
    availability: Literal["available", "expired"] = "available"
    expired_at: datetime | None = None
    expiration_reason: str | None = None


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
    event: EventResponse | None = None
    bounding_box: dict[str, float] | None = None
    target_type: str | None = None
