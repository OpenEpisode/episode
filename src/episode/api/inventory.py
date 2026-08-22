from __future__ import annotations

import ipaddress
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from episode.domain.models import CapabilityConfig, Device

RecordingMode = Literal["disabled", "on_event", "on_episode"]
DeviceType = Literal["camera", "doorbell", "alarm_panel", "sensor", "other"]
AuthMode = Literal["digest_wsse", "digest"]


class AreaCreateRequest(BaseModel):
    id: str | None = Field(default=None, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=80)
    location: str = Field(default="", max_length=200)

    @field_validator("name", "location")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class AreaUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    location: str = Field(default="", max_length=200)
    enabled: bool = True

    @field_validator("name", "location")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class VideoConfigurationRequest(BaseModel):
    enabled: bool = True
    manual_endpoint: bool = False
    protocol: str = Field(default="rtsp", max_length=16)
    port: int | None = Field(default=554, ge=1, le=65535)
    path: str = Field(default="/Streaming/Channels/101", max_length=500)
    recording_mode: RecordingMode = "on_event"


class ONVIFConfigurationRequest(BaseModel):
    enabled: bool = True
    protocol: str = Field(default="http", max_length=16)
    port: int | None = Field(default=80, ge=1, le=65535)
    path: str = Field(default="/onvif/device_service", max_length=500)
    auth_mode: AuthMode = "digest_wsse"
    events_enabled: bool = False
    onvif_relaxed_validation: bool = False


class ISAPIConfigurationRequest(BaseModel):
    enabled: bool = False
    protocol: str = Field(default="http", max_length=16)
    port: int | None = Field(default=80, ge=1, le=65535)
    path: str = Field(default="/ISAPI/Event/notification/alertStream", max_length=500)
    ignore_events: list[str] = Field(default_factory=lambda: ["videoloss", "illaccess"])

    @field_validator("ignore_events")
    @classmethod
    def normalize_ignored_events(cls, values: list[str]) -> list[str]:
        return sorted({value.strip().lower() for value in values if value.strip()})


class SDKConfigurationRequest(BaseModel):
    enabled: bool = False
    port: int = Field(default=8000, ge=1, le=65535)


class EpisodePolicyRequest(BaseModel):
    activity_window_seconds: int | None = Field(default=None, ge=1, le=3600)


class DeviceWriteRequest(BaseModel):
    id: str | None = Field(default=None, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=80)
    device_type: DeviceType = "camera"
    area_id: str = Field(min_length=1, max_length=64)
    enabled: bool = True
    ip_address: str = Field(default="", max_length=255)
    username: str | None = Field(default=None, max_length=128)
    password: str | None = Field(default=None, max_length=256)
    clear_credentials: bool = False
    episode_policy: EpisodePolicyRequest = Field(default_factory=EpisodePolicyRequest)
    video: VideoConfigurationRequest = Field(default_factory=VideoConfigurationRequest)
    onvif: ONVIFConfigurationRequest = Field(default_factory=ONVIFConfigurationRequest)
    isapi: ISAPIConfigurationRequest = Field(default_factory=ISAPIConfigurationRequest)
    hikvision_sdk: SDKConfigurationRequest = Field(default_factory=SDKConfigurationRequest)

    @field_validator("name", "device_type", "area_id", "ip_address")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_network_configuration(self):
        needs_address = any(
            (
                self.video.enabled,
                self.onvif.enabled,
                self.isapi.enabled,
                self.hikvision_sdk.enabled,
            )
        )
        if needs_address and not self.ip_address:
            raise ValueError("Network address is required for enabled integrations")
        if self.ip_address:
            try:
                ipaddress.ip_address(self.ip_address)
            except ValueError as exc:
                raise ValueError("Network address must be a valid IPv4 or IPv6 address") from exc
        for value in (self.video, self.onvif, self.isapi):
            if value.enabled and value.path and not value.path.startswith("/"):
                raise ValueError("Integration paths must start with '/'")
        if self.video.enabled and not self.onvif.enabled and not self.video.manual_endpoint:
            raise ValueError(
                "Enable a manual RTSP endpoint when video recording is used without ONVIF"
            )
        if self.video.manual_endpoint and (not self.video.protocol or not self.video.path):
            raise ValueError("A manual RTSP endpoint requires a protocol and path")
        return self


SupportStatus = Literal[
    "supported",
    "unsupported",
    "authentication_failed",
    "unreachable",
    "unavailable",
    "not_validated",
]


class IntegrationSupportResponse(BaseModel):
    status: SupportStatus
    summary: str
    checked_at: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class DeviceValidationResponse(BaseModel):
    device_id: str | None = None
    results: dict[str, IntegrationSupportResponse]


class DeviceConfigurationResponse(BaseModel):
    username_configured: bool
    password_configured: bool
    episode_policy: EpisodePolicyRequest
    video: VideoConfigurationRequest
    onvif: ONVIFConfigurationRequest
    isapi: ISAPIConfigurationRequest
    hikvision_sdk: SDKConfigurationRequest


def editable_device_configuration(device: Device) -> dict:
    video = device.get_config("video")
    onvif = device.get_config("onvif")
    isapi = device.get_config("isapi")
    sdk = device.get_config("hikvision_sdk")
    discovered_video = bool(video and video.settings.get("origin") == "onvif")
    manual_video = bool(video and video.protocol and video.path and not discovered_video)
    return DeviceConfigurationResponse(
        username_configured=bool(device.username),
        password_configured=bool(device.password),
        episode_policy=EpisodePolicyRequest(
            activity_window_seconds=device.activity_window_seconds,
        ),
        video=VideoConfigurationRequest(
            enabled=video is not None,
            manual_endpoint=manual_video,
            protocol=video.protocol if manual_video else "rtsp",
            port=video.port if manual_video else 554,
            path=video.path if manual_video else "/Streaming/Channels/101",
            recording_mode=(
                video.settings.get("recording_mode", "on_event") if video else "disabled"
            ),
        ),
        onvif=ONVIFConfigurationRequest(
            enabled=onvif is not None,
            protocol=onvif.protocol if onvif else "http",
            port=onvif.port if onvif else 80,
            path=onvif.path if onvif else "/onvif/device_service",
            auth_mode=onvif.settings.get("auth_mode", "digest_wsse") if onvif else "digest_wsse",
            events_enabled=bool(onvif.settings.get("events_enabled", False)) if onvif else False,
            onvif_relaxed_validation=bool(onvif.settings.get("onvif_relaxed_validation", False))
            if onvif
            else False,
        ),
        isapi=ISAPIConfigurationRequest(
            enabled=isapi is not None,
            protocol=isapi.protocol if isapi else "http",
            port=isapi.port if isapi else 80,
            path=isapi.path if isapi else "/ISAPI/Event/notification/alertStream",
            ignore_events=list(isapi.settings.get("ignore_events", [])) if isapi else [],
        ),
        hikvision_sdk=SDKConfigurationRequest(
            enabled=sdk is not None,
            port=sdk.port if sdk and sdk.port else 8000,
        ),
    ).model_dump()


def device_from_request(
    device_id: str,
    request: DeviceWriteRequest,
    existing: Device | None = None,
) -> Device:
    managed_capabilities = {"doorbell", "onvif", "isapi", "hikvision_sdk"}
    capabilities = set(existing.capabilities if existing else ()) - managed_capabilities

    configs = {
        key: value
        for key, value in (existing.configs.items() if existing else ())
        if key not in {"video", "onvif", "isapi", "hikvision_sdk"}
    }
    if request.video.enabled:
        previous = existing.get_config("video") if existing else None
        settings = dict(previous.settings) if previous else {}
        settings.pop("origin", None)
        settings.pop("profile_token", None)
        settings["recording_mode"] = request.video.recording_mode
        configs["video"] = CapabilityConfig(
            protocol=request.video.protocol if request.video.manual_endpoint else "",
            port=request.video.port if request.video.manual_endpoint else None,
            path=request.video.path if request.video.manual_endpoint else "",
            settings=settings,
        )
    if request.onvif.enabled:
        previous = existing.get_config("onvif") if existing else None
        settings = dict(previous.settings) if previous else {}
        settings.update(
            auth_mode=request.onvif.auth_mode,
            events_enabled=request.onvif.events_enabled,
            onvif_relaxed_validation=request.onvif.onvif_relaxed_validation,
        )
        configs["onvif"] = CapabilityConfig(
            protocol=request.onvif.protocol,
            port=request.onvif.port,
            path=request.onvif.path,
            settings=settings,
        )
    if request.isapi.enabled:
        configs["isapi"] = CapabilityConfig(
            protocol=request.isapi.protocol,
            port=request.isapi.port,
            path=request.isapi.path,
            settings={"ignore_events": request.isapi.ignore_events},
        )
    if request.hikvision_sdk.enabled:
        configs["hikvision_sdk"] = CapabilityConfig(port=request.hikvision_sdk.port)

    if request.clear_credentials:
        username = ""
        password = ""
    else:
        username = (
            request.username
            if request.username is not None
            else existing.username
            if existing
            else ""
        )
        password = (
            request.password
            if request.password is not None
            else existing.password
            if existing
            else ""
        )

    return Device(
        id=device_id,
        name=request.name,
        device_type=request.device_type,
        area_id=request.area_id,
        capabilities=sorted(capabilities),
        ip_address=request.ip_address,
        username=username,
        password=password,
        configs=configs,
        activity_window_seconds=request.episode_policy.activity_window_seconds,
        metadata=dict(existing.metadata) if existing else {},
        enabled=request.enabled,
    )


def validation_device_from_request(
    request: DeviceWriteRequest,
    existing: Device | None = None,
) -> Device:
    """Build a probe target while retaining disabled integration endpoint values."""
    device = device_from_request(request.id or "validation", request, existing)
    device.configs["video"] = CapabilityConfig(
        protocol=request.video.protocol if request.video.manual_endpoint else "",
        port=request.video.port if request.video.manual_endpoint else None,
        path=request.video.path if request.video.manual_endpoint else "",
        settings={
            "recording_mode": request.video.recording_mode,
            "manual_endpoint": request.video.manual_endpoint,
        },
    )
    device.configs["onvif"] = CapabilityConfig(
        protocol=request.onvif.protocol,
        port=request.onvif.port,
        path=request.onvif.path,
        settings={
            "auth_mode": request.onvif.auth_mode,
            "events_enabled": request.onvif.events_enabled,
            "onvif_relaxed_validation": request.onvif.onvif_relaxed_validation,
        },
    )
    device.configs["isapi"] = CapabilityConfig(
        protocol=request.isapi.protocol,
        port=request.isapi.port,
        path=request.isapi.path,
        settings={"ignore_events": request.isapi.ignore_events},
    )
    device.configs["hikvision_sdk"] = CapabilityConfig(port=request.hikvision_sdk.port)
    return device
