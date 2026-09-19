from __future__ import annotations

import ipaddress
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from episode.domain.event_filter import LEGACY_FILTER_CLASSES, validate_selector
from episode.domain.models import CapabilityConfig, Device

RecordingMode = Literal["disabled", "on_event", "on_episode"]
DeviceType = Literal["camera", "doorbell", "alarm_panel", "sensor", "other"]
AuthMode = Literal["digest_wsse", "digest"]
GenericEventFilter = Literal["inherit", "enabled", "disabled"]


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
    relaxed_xml: bool = False


class ISAPIConfigurationRequest(BaseModel):
    enabled: bool = False
    protocol: str = Field(default="http", max_length=16)
    port: int | None = Field(default=80, ge=1, le=65535)
    path: str = Field(default="/ISAPI/Event/notification/alertStream", max_length=500)
    # Empty by default: these names stop the plugin *interpreting* a vendor
    # message, so nothing becomes a canonical Event and no core filter decision
    # is ever recorded. Suppressing noise belongs in ``event_filter`` instead.
    ignore_events: list[str] = Field(default_factory=list)

    @field_validator("ignore_events")
    @classmethod
    def normalize_ignored_events(cls, values: list[str]) -> list[str]:
        return sorted({value.strip().lower() for value in values if value.strip()})


class SDKConfigurationRequest(BaseModel):
    enabled: bool = False
    port: int = Field(default=8000, ge=1, le=65535)


class ReolinkConfigurationRequest(BaseModel):
    enabled: bool = False
    host: str = Field(default="", max_length=255)
    port: int | None = Field(default=9000, ge=1, le=65535)
    media_enabled: bool = False
    events_enabled: bool = False

    @field_validator("host")
    @classmethod
    def strip_host(cls, value: str) -> str:
        return value.strip()


class EpisodePolicyRequest(BaseModel):
    activity_window_seconds: int | None = Field(default=None, ge=1, le=3600)
    # Event classes this Device suppresses. ``null`` inherits the active Capture
    # Profile; ``[]`` is the explicit negative "this camera filters nothing".
    event_filter: list[str] | None = None
    # Deprecated: superseded by ``event_filter``. Accepted for one release and
    # translated, so ``beta.7`` clients keep working. Ignored when both are sent.
    generic_event_filter: GenericEventFilter = "inherit"

    @field_validator("event_filter")
    @classmethod
    def validate_event_filter(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        try:
            # A Device may select every class; profiles offer the same set.
            return validate_selector(value, level="device")
        except ValueError as error:
            raise ValueError(str(error)) from error


class DeviceWriteRequest(BaseModel):
    id: str | None = Field(default=None, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=80)
    device_type: DeviceType = "camera"
    area_id: str = Field(min_length=1, max_length=64)
    enabled: bool = True
    setup_state: Literal["ready", "needs_setup"] = "ready"
    # Operator-provided fallback when protocol discovery cannot identify the
    # manufacturer.  This is a hint for catalogue filtering, not proof of
    # protocol support.
    manufacturer: str | None = Field(default=None, max_length=100)
    ip_address: str = Field(default="", max_length=255)
    username: str | None = Field(default=None, max_length=128)
    password: str | None = Field(default=None, max_length=256)
    clear_credentials: bool = False
    episode_policy: EpisodePolicyRequest = Field(default_factory=EpisodePolicyRequest)
    video: VideoConfigurationRequest = Field(default_factory=VideoConfigurationRequest)
    onvif: ONVIFConfigurationRequest = Field(default_factory=ONVIFConfigurationRequest)
    isapi: ISAPIConfigurationRequest = Field(default_factory=ISAPIConfigurationRequest)
    hikvision_sdk: SDKConfigurationRequest = Field(default_factory=SDKConfigurationRequest)
    reolink: ReolinkConfigurationRequest = Field(default_factory=ReolinkConfigurationRequest)
    # Validation is deliberately opt-in for vendor integrations.  ``None``
    # means the initial generic ONVIF-only probe; an empty list means no probe.
    integration_ids: list[str] | None = Field(default=None, max_length=20)

    @field_validator("name", "device_type", "area_id", "ip_address")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("manufacturer")
    @classmethod
    def strip_manufacturer(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("integration_ids")
    @classmethod
    def validate_integration_ids(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        if any(
            not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", value)
            for value in values
        ):
            raise ValueError("integration IDs must use bounded lowercase identifiers")
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def validate_network_configuration(self):
        needs_address = any(
            (
                self.video.enabled,
                self.onvif.enabled,
                self.isapi.enabled,
                self.hikvision_sdk.enabled,
                self.reolink.enabled,
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
    setup_state: Literal["ready", "needs_setup"] = "ready"
    manufacturer: str | None = None
    username_configured: bool
    password_configured: bool
    episode_policy: EpisodePolicyRequest
    video: VideoConfigurationRequest
    onvif: ONVIFConfigurationRequest
    isapi: ISAPIConfigurationRequest
    hikvision_sdk: SDKConfigurationRequest
    reolink: ReolinkConfigurationRequest


def editable_device_configuration(device: Device) -> dict:
    video = device.get_config("video")
    onvif = device.get_config("onvif")
    isapi = device.get_config("isapi")
    sdk = device.get_config("hikvision_sdk")
    reolink = device.get_config("reolink")
    discovered_video = bool(video and video.settings.get("origin") == "onvif")
    manual_video = bool(video and video.protocol and video.path and not discovered_video)
    return DeviceConfigurationResponse(
        setup_state=getattr(device, "setup_state", "ready"),
        manufacturer=device.metadata.get("_manufacturer_override"),
        username_configured=bool(device.username),
        password_configured=bool(device.password),
        episode_policy=EpisodePolicyRequest(
            activity_window_seconds=device.activity_window_seconds,
            event_filter=(list(device.event_filter) if device.event_filter is not None else None),
            # Deprecated mirror of ``event_filter`` for one release.
            generic_event_filter=legacy_generic_event_filter(device.event_filter),
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
            relaxed_xml=bool(onvif.settings.get("relaxed_xml", False)) if onvif else False,
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
        reolink=ReolinkConfigurationRequest(
            enabled=reolink is not None,
            host=reolink.settings.get("host", "") if reolink else "",
            port=reolink.port if reolink and reolink.port else 9000,
            media_enabled=bool(reolink.settings.get("media_enabled", False)) if reolink else False,
            events_enabled=bool(reolink.settings.get("events_enabled", False))
            if reolink
            else False,
        ),
    ).model_dump()


def resolve_device_event_filter(
    event_filter: list[str] | None,
    generic_event_filter: GenericEventFilter,
) -> list[str] | None:
    """Resolve the Device selector, preferring the class field over the legacy one."""
    if event_filter is not None:
        return sorted(event_filter)
    return legacy_device_selector(generic_event_filter)


def legacy_device_selector(generic_event_filter: GenericEventFilter) -> list[str] | None:
    """Translate the ``beta.7`` Device tri-state to a class selector (or inherit)."""
    if generic_event_filter == "inherit":
        return None
    if generic_event_filter == "enabled":
        return sorted(LEGACY_FILTER_CLASSES)
    return []


def legacy_generic_event_filter(event_filter: list[str] | None) -> GenericEventFilter:
    """Project a class selector back to the deprecated tri-state for responses."""
    if event_filter is None:
        return "inherit"
    return "enabled" if event_filter else "disabled"


def device_from_request(
    device_id: str,
    request: DeviceWriteRequest,
    existing: Device | None = None,
) -> Device:
    managed_capabilities = {"doorbell", "onvif", "isapi", "hikvision_sdk", "reolink"}
    capabilities = set(existing.capabilities if existing else ()) - managed_capabilities

    configs = {
        key: value
        for key, value in (existing.configs.items() if existing else ())
        if key not in {"video", "onvif", "isapi", "hikvision_sdk", "reolink"}
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
            relaxed_xml=request.onvif.relaxed_xml,
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
    if request.reolink.enabled:
        previous = existing.get_config("reolink") if existing else None
        settings = dict(previous.settings) if previous else {}
        settings["host"] = request.reolink.host
        settings["media_enabled"] = request.reolink.media_enabled
        settings["events_enabled"] = request.reolink.events_enabled
        configs["reolink"] = CapabilityConfig(
            port=request.reolink.port,
            settings=settings,
        )

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

    metadata = dict(existing.metadata) if existing else {}
    if "manufacturer" in request.model_fields_set:
        if request.manufacturer and request.manufacturer.strip():
            metadata["_manufacturer_override"] = request.manufacturer.strip()
        else:
            metadata.pop("_manufacturer_override", None)

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
        metadata=metadata,
        enabled=request.enabled,
        event_filter=resolve_device_event_filter(
            request.episode_policy.event_filter,
            request.episode_policy.generic_event_filter,
        ),
        setup_state=request.setup_state,
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
            "relaxed_xml": request.onvif.relaxed_xml,
        },
    )
    device.configs["isapi"] = CapabilityConfig(
        protocol=request.isapi.protocol,
        port=request.isapi.port,
        path=request.isapi.path,
        settings={"ignore_events": request.isapi.ignore_events},
    )
    device.configs["hikvision_sdk"] = CapabilityConfig(port=request.hikvision_sdk.port)
    device.configs["reolink"] = CapabilityConfig(
        port=request.reolink.port,
        settings={
            "host": request.reolink.host,
            "media_enabled": request.reolink.media_enabled,
            "events_enabled": request.reolink.events_enabled,
        },
    )
    return device
