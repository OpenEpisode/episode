from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field

from episode.api.inventory import (
    DeviceConfigurationResponse,
    IntegrationSupportResponse,
    editable_device_configuration,
)
from episode.domain.models import Device

OperationalState = Literal["healthy", "degraded", "unavailable", "disabled", "unknown"]

_DEVICE_INTEGRATION_FLAGS = {"onvif", "isapi", "hikvision_sdk"}


def product_capabilities(capabilities: Sequence[str]) -> list[str]:
    """Return user-facing capabilities without integration activation flags."""
    return sorted(set(capabilities) - _DEVICE_INTEGRATION_FLAGS)


class IntegrationResponse(BaseModel):
    id: str
    name: str
    type: str
    kind: Literal["device", "shared", "plugin"]
    state: OperationalState
    device_id: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    summary: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class DeviceIdentityResponse(BaseModel):
    manufacturer: str | None = None
    model: str | None = None
    firmware_version: str | None = None


class CapturePolicyResponse(BaseModel):
    recording: str
    automatic_snapshots: bool
    onvif_events: bool | None = None


class DeviceSummaryResponse(BaseModel):
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


class ServiceResponse(BaseModel):
    id: str
    name: str
    state: OperationalState
    summary: str
    metrics: dict[str, Any] = Field(default_factory=dict)


class IntegrationCountsResponse(BaseModel):
    total: int = 0
    healthy: int = 0
    degraded: int = 0
    unavailable: int = 0


class SystemStatusResponse(BaseModel):
    version: str
    state: OperationalState
    active_recordings: int = 0
    restart_required: bool = False
    services: dict[str, OperationalState]
    integrations: IntegrationCountsResponse


class DiagnosticsResponse(BaseModel):
    status: SystemStatusResponse
    services: list[ServiceResponse]
    integrations: list[IntegrationResponse]


def _slug(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-") or "integration"


def _state_for_connector(status: Mapping[str, Any]) -> OperationalState:
    if not status.get("running", False):
        return "unavailable"
    if status.get("healthy") is False:
        return "degraded"
    if status.get("stream_active") is False:
        return "degraded"
    if status.get("connected") is False:
        return "degraded"
    if status.get("last_error"):
        return "degraded"
    return "healthy"


def _state_for_plugin(value: object) -> OperationalState:
    state = str(value or "")
    if state in {"ready", "running"}:
        return "healthy"
    if state in {"degraded", "validating", "starting"}:
        return "degraded"
    if state in {"failed", "not_installed", "incomplete", "incompatible", "stopped"}:
        return "unavailable"
    return "unknown"


def _integration_capabilities(kind: str, status: Mapping[str, Any]) -> list[str]:
    if kind == "onvif":
        capabilities = ["discovery", "media"]
        if any(profile.get("snapshot") for profile in status.get("profiles", [])):
            capabilities.append("snapshots")
        if status.get("event_topics") or status.get("events_enabled"):
            capabilities.append("events")
        return capabilities
    return {
        "isapi": ["events"],
        "alarm_server": ["events"],
        "ftp": ["evidence-upload"],
        "hikvision_sdk": ["events", "device-information"],
    }.get(kind, [])


def _connector_summary(kind: str, status: Mapping[str, Any], state: OperationalState) -> str:
    if state != "healthy":
        return str(status.get("last_error") or "Unavailable")
    if kind == "onvif":
        profiles = len(status.get("profiles", []))
        event_policy = "events enabled" if status.get("events_enabled") else "events disabled"
        return (
            f"Connected · {profiles} media profile{'s' if profiles != 1 else ''} · {event_policy}"
        )
    if kind == "isapi":
        return "Event stream connected"
    if kind == "alarm_server":
        return f"{int(status.get('requests_handled', 0))} deliveries accepted"
    if kind == "ftp":
        return f"Listening on port {status.get('port', '-')}"
    return "Running"


def _connector_details(kind: str, status: Mapping[str, Any]) -> dict[str, Any]:
    keys = {
        "onvif": (
            "connected",
            "subscribed",
            "events_enabled",
            "profiles",
            "selected_profile",
            "event_topics",
            "events_received",
            "events_suppressed",
            "last_event",
            "last_error",
        ),
        "isapi": ("stream_active", "last_event", "last_error"),
        "alarm_server": ("path", "port", "requests_handled", "requests_rejected"),
        "ftp": ("host", "port", "passive_ports"),
    }.get(kind, ("last_error",))
    return {key: status[key] for key in keys if key in status and status[key] is not None}


class OperationalView:
    """Project internal runtime state into stable, UI-facing API resources."""

    def __init__(
        self,
        *,
        version: str,
        engine_status: Callable[[], Mapping[str, Any]],
        recorder_status: Callable[[], Mapping[str, Any]],
        snapshot_status: Callable[[], Mapping[str, Any]],
        connector_statuses: Callable[[], Sequence[Mapping[str, Any]]],
        plugin_statuses: Callable[[], Sequence[Mapping[str, Any]]],
        snapshots_enabled: bool,
        restart_required: Callable[[], bool] = lambda: False,
    ) -> None:
        self._version = version
        self._engine_status = engine_status
        self._recorder_status = recorder_status
        self._snapshot_status = snapshot_status
        self._connector_statuses = connector_statuses
        self._plugin_statuses = plugin_statuses
        self._snapshots_enabled = snapshots_enabled
        self._restart_required = restart_required

    def _connectors(self) -> list[dict[str, Any]]:
        return [dict(status) for status in self._connector_statuses()]

    def _plugins(self) -> list[dict[str, Any]]:
        return [dict(status) for status in self._plugin_statuses()]

    def integrations(self, *, detailed: bool) -> list[dict[str, Any]]:
        integrations = [
            self._connector_integration(status, detailed=detailed) for status in self._connectors()
        ]
        integrations.extend(
            self._plugin_integration(plugin, detailed=detailed) for plugin in self._plugins()
        )
        return integrations

    def status(self) -> dict[str, Any]:
        engine = dict(self._engine_status())
        recorder = dict(self._recorder_status())
        snapshots = dict(self._snapshot_status())
        service_states: dict[str, OperationalState] = {
            "engine": "healthy" if engine.get("running") else "unavailable",
            "recorder": "healthy" if recorder.get("running") else "unavailable",
            "snapshots": (
                "disabled"
                if not self._snapshots_enabled
                else "healthy"
                if snapshots.get("running")
                else "unavailable"
            ),
        }
        integrations = self.integrations(detailed=False)
        counts = {
            "total": len(integrations),
            "healthy": sum(item["state"] == "healthy" for item in integrations),
            "degraded": sum(item["state"] == "degraded" for item in integrations),
            "unavailable": sum(item["state"] == "unavailable" for item in integrations),
        }
        if any(service_states[name] == "unavailable" for name in ("engine", "recorder")):
            state: OperationalState = "unavailable"
        elif counts["degraded"] or counts["unavailable"]:
            state = "degraded"
        else:
            state = "healthy"
        return {
            "version": self._version,
            "state": state,
            "active_recordings": int(recorder.get("active_recordings", 0)),
            "restart_required": self._restart_required(),
            "services": service_states,
            "integrations": counts,
        }

    def diagnostics(self) -> dict[str, Any]:
        engine = dict(self._engine_status())
        recorder = dict(self._recorder_status())
        snapshots = dict(self._snapshot_status())
        status = self.status()
        services = [
            {
                "id": "engine",
                "name": "Episode engine",
                "state": status["services"]["engine"],
                "summary": f"{int(engine.get('timeout', 0))}s inactivity timeout",
                "metrics": {},
            },
            {
                "id": "recorder",
                "name": "Recorder",
                "state": status["services"]["recorder"],
                "summary": f"{int(recorder.get('active_recordings', 0))} active recordings",
                "metrics": {
                    "active_recordings": int(recorder.get("active_recordings", 0)),
                    "cameras": int(recorder.get("cameras", 0)),
                    "segment_seconds": int(recorder.get("segment_seconds", 0)),
                },
            },
            {
                "id": "snapshots",
                "name": "Automatic snapshots",
                "state": status["services"]["snapshots"],
                "summary": "Enabled" if self._snapshots_enabled else "Disabled by policy",
                "metrics": {
                    key: int(snapshots.get(key, 0))
                    for key in ("captured", "failures", "suppressed", "active")
                },
            },
        ]
        return {
            "status": status,
            "services": services,
            "integrations": self.integrations(detailed=True),
        }

    def device_summary(self, device: Device) -> dict[str, Any]:
        integrations = self._device_integrations(device, detailed=False)
        return {
            "id": device.id,
            "name": device.name,
            "device_type": device.device_type,
            "area_id": device.area_id,
            "enabled": device.enabled,
            "capabilities": product_capabilities(device.capabilities),
            "state": "disabled" if not device.enabled else self._device_state(integrations),
            "identity": self._device_identity(device),
            "integrations": integrations,
        }

    def device_detail(self, device: Device) -> dict[str, Any]:
        summary = self.device_summary(device)
        integrations = self._device_integrations(device, detailed=True)
        video = device.get_config("video")
        onvif = device.get_config("onvif")
        recording = (
            str(video.settings.get("recording_mode", "on_event")) if video else "unavailable"
        )
        events_enabled = bool(onvif.settings.get("events_enabled", False)) if onvif else None
        return {
            **summary,
            "integrations": integrations,
            "ip_address": device.ip_address,
            "configuration": editable_device_configuration(device),
            "can_delete": False,
            "capture_policy": {
                "recording": recording,
                "automatic_snapshots": self._snapshots_enabled,
                "onvif_events": events_enabled,
            },
        }

    def _device_integrations(self, device: Device, *, detailed: bool) -> list[dict[str, Any]]:
        integrations = [
            self._connector_integration(status, detailed=detailed)
            for status in self._connectors()
            if status.get("device_id") == device.id
        ]
        for plugin in self._plugins():
            integrations.extend(
                self._plugin_instance_integration(plugin, instance, detailed=detailed)
                for instance in plugin.get("instances", [])
                if instance.get("id") == device.id
            )

        present = {item["type"] for item in integrations}
        for capability, integration_type in {
            "onvif": "onvif",
            "isapi": "isapi",
            "hikvision_sdk": "hikvision_sdk",
        }.items():
            if capability not in device.capabilities or integration_type in present:
                continue
            integrations.append(
                {
                    "id": f"{integration_type}:{device.id}",
                    "name": {
                        "onvif": "ONVIF",
                        "isapi": "ISAPI",
                        "hikvision_sdk": "Hikvision HCNetSDK",
                    }[integration_type],
                    "type": integration_type,
                    "kind": "device",
                    "state": "unavailable",
                    "device_id": device.id,
                    "capabilities": _integration_capabilities(integration_type, {}),
                    "summary": "Configured but unavailable",
                    "details": {},
                }
            )
        if not device.enabled:
            for integration in integrations:
                integration["state"] = "disabled"
                integration["summary"] = "Disabled in inventory"
        return sorted(integrations, key=lambda item: (item["type"], item["id"]))

    @staticmethod
    def _device_state(integrations: Sequence[Mapping[str, Any]]) -> OperationalState:
        states = [item.get("state") for item in integrations]
        if not states:
            return "unknown"
        if all(state == "healthy" for state in states):
            return "healthy"
        if any(state in {"healthy", "degraded"} for state in states):
            return "degraded"
        return "unavailable"

    def _device_identity(self, device: Device) -> dict[str, str | None]:
        candidates: list[Mapping[str, Any]] = [
            status
            for status in self._connectors()
            if status.get("device_id") == device.id and status.get("type") == "onvif"
        ]
        for plugin in self._plugins():
            candidates.extend(
                instance.get("device_info") or {}
                for instance in plugin.get("instances", [])
                if instance.get("id") == device.id
            )
        metadata = device.metadata.get("onvif", {})
        if isinstance(metadata, Mapping):
            candidates.append(metadata)

        def first(key: str) -> str | None:
            return next((str(item[key]) for item in candidates if item.get(key)), None)

        return {
            "manufacturer": first("manufacturer"),
            "model": first("model"),
            "firmware_version": first("firmware_version"),
        }

    @staticmethod
    def _connector_integration(
        status: Mapping[str, Any],
        *,
        detailed: bool,
    ) -> dict[str, Any]:
        kind = str(status.get("type") or "connector")
        state = _state_for_connector(status)
        device_id = str(status.get("device_id") or "") or None
        return {
            "id": f"{kind}:{device_id or _slug(status.get('name'))}",
            "name": str(status.get("name") or kind.replace("_", " ").title()),
            "type": kind,
            "kind": "device" if device_id else "shared",
            "state": state,
            "device_id": device_id,
            "capabilities": _integration_capabilities(kind, status),
            "summary": _connector_summary(kind, status, state),
            "details": _connector_details(kind, status) if detailed else {},
        }

    @staticmethod
    def _plugin_integration(
        plugin: Mapping[str, Any],
        *,
        detailed: bool,
    ) -> dict[str, Any]:
        state = _state_for_plugin(plugin.get("state"))
        instances = list(plugin.get("instances", []))
        summary = str(plugin.get("error") or str(plugin.get("state", "")).replace("_", " ").title())
        if instances:
            running = sum(_state_for_plugin(item.get("state")) == "healthy" for item in instances)
            summary = f"{running}/{len(instances)} instances running"
        details = {}
        if detailed:
            details = {
                "version": plugin.get("version"),
                "architecture": plugin.get("architecture"),
                "metrics": plugin.get("metrics", {}),
                "instances": [
                    {
                        "id": item.get("id"),
                        "name": item.get("name"),
                        "state": str(item.get("state")),
                        "messages_received": item.get("messages_received", 0),
                        "last_message_at": item.get("last_message_at"),
                        "error": item.get("error"),
                    }
                    for item in instances
                ],
            }
        plugin_type = str(plugin.get("id") or "plugin").replace("-", "_")
        return {
            "id": str(plugin.get("id") or "plugin"),
            "name": str(plugin.get("name") or "Plugin"),
            "type": plugin_type,
            "kind": "plugin",
            "state": state,
            "device_id": None,
            "capabilities": _integration_capabilities(plugin_type, plugin),
            "summary": summary,
            "details": details,
        }

    @staticmethod
    def _plugin_instance_integration(
        plugin: Mapping[str, Any],
        instance: Mapping[str, Any],
        *,
        detailed: bool,
    ) -> dict[str, Any]:
        plugin_type = str(plugin.get("id") or "plugin").replace("-", "_")
        state = _state_for_plugin(instance.get("state"))
        messages = int(instance.get("messages_received", 0))
        summary = str(instance.get("error") or f"Connected · {messages} notifications")
        details = {}
        if detailed:
            details = {
                "messages_received": messages,
                "connected_at": instance.get("connected_at"),
                "last_message_at": instance.get("last_message_at"),
                "error": instance.get("error"),
            }
        return {
            "id": f"{plugin_type}:{instance.get('id')}",
            "name": str(plugin.get("name") or plugin_type),
            "type": plugin_type,
            "kind": "device",
            "state": state,
            "device_id": str(instance.get("id") or "") or None,
            "capabilities": _integration_capabilities(plugin_type, plugin),
            "summary": summary,
            "details": details,
        }
