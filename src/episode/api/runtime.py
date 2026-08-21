from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from episode.api.inventory import editable_device_configuration
from episode.api.schemas import OperationalState
from episode.domain.models import Device


def product_capabilities(capabilities: Sequence[str]) -> list[str]:
    """Return user-facing capabilities without integration activation flags."""
    return sorted(set(capabilities) - {"doorbell", "onvif", "isapi", "hikvision_sdk"})


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
    return {
        "alarm_server": ["events"],
        "event_api": ["event-input"],
        "ftp": ["evidence-upload"],
    }.get(kind, [])


def _connector_summary(kind: str, status: Mapping[str, Any], state: OperationalState) -> str:
    if state != "healthy":
        return str(status.get("last_error") or "Unavailable")
    if kind == "alarm_server":
        return f"{int(status.get('requests_handled', 0))} deliveries accepted"
    if kind == "event_api":
        return (
            f"{int(status.get('events_accepted', 0))} Events accepted · "
            f"{int(status.get('duplicates', 0))} duplicates"
        )
    if kind == "ftp":
        return (
            f"Listening on port {status.get('port', '-')} · "
            f"{int(status.get('uploads_received', 0))} uploads preserved"
        )
    return "Running"


def _connector_details(kind: str, status: Mapping[str, Any]) -> dict[str, Any]:
    keys = {
        "alarm_server": ("path", "port", "requests_handled", "requests_rejected"),
        "event_api": (
            "path",
            "port",
            "requests_handled",
            "events_accepted",
            "duplicates",
            "requests_rejected",
            "unmatched",
            "last_event_at",
            "handler_failures",
            "handler_timeouts",
            "last_error",
        ),
        "ftp": (
            "host",
            "port",
            "passive_ports",
            "uploads_received",
            "uploads_failed",
            "last_upload_at",
            "last_error",
        ),
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
    ) -> None:
        self._version = version
        self._engine_status = engine_status
        self._recorder_status = recorder_status
        self._snapshot_status = snapshot_status
        self._connector_statuses = connector_statuses
        self._plugin_statuses = plugin_statuses
        self._snapshots_enabled = snapshots_enabled

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
        plugins = self._plugins()
        for plugin in plugins:
            integrations.extend(
                self._plugin_instance_integration(plugin, instance, detailed=detailed)
                for instance in plugin.get("instances", [])
                if instance.get("id") == device.id
            )

        present = {item["type"] for item in integrations}
        for plugin in plugins:
            metadata = plugin.get("integration")
            if not isinstance(metadata, Mapping) or not metadata.get("device_scoped"):
                continue
            configured_device_ids = set(metadata.get("configured_device_ids") or [])
            integration_type = str(metadata.get("type") or "").replace("-", "_")
            explicitly_assigned = device.id in configured_device_ids
            configured = integration_type in device.configs or explicitly_assigned
            if not configured or integration_type in present:
                continue
            state = _state_for_plugin(plugin.get("state")) if explicitly_assigned else "unavailable"
            summary = (
                str(plugin.get("error") or plugin.get("summary") or "Configured")
                if explicitly_assigned
                else "Configured but unavailable"
            )
            integrations.append(
                {
                    "id": f"{integration_type}:{device.id}",
                    "name": str(metadata.get("name") or plugin.get("name") or integration_type),
                    "type": integration_type,
                    "kind": "device",
                    "state": state,
                    "device_id": device.id,
                    "capabilities": list(metadata.get("capabilities") or []),
                    "summary": summary,
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
        candidates: list[Mapping[str, Any]] = []
        candidates.extend(
            status
            for status in self._connectors()
            if status.get("device_id") == device.id
            and any(status.get(key) for key in ("manufacturer", "model", "firmware_version"))
        )
        for plugin in self._plugins():
            candidates.extend(
                instance.get("device_info") or {}
                for instance in plugin.get("instances", [])
                if instance.get("id") == device.id
            )
        candidates.extend(
            value
            for value in device.metadata.values()
            if isinstance(value, Mapping)
            and any(value.get(key) for key in ("manufacturer", "model", "firmware_version"))
        )

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
        metadata = plugin.get("integration")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        plugin_type = str(metadata.get("type") or plugin.get("id") or "plugin").replace("-", "_")
        capabilities = list(metadata.get("capabilities") or [])
        state = _state_for_plugin(plugin.get("state"))
        instances = list(plugin.get("instances", []))
        summary = str(
            plugin.get("error")
            or plugin.get("summary")
            or str(plugin.get("state", "")).replace("_", " ").title()
        )
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
                        "summary": item.get("summary"),
                        "capabilities": list(item.get("capabilities") or []),
                        "details": dict(item.get("details") or {}),
                    }
                    for item in instances
                ],
            }
        return {
            "id": str(plugin.get("id") or "plugin"),
            "name": str(metadata.get("name") or plugin.get("name") or "Plugin"),
            "type": plugin_type,
            "kind": "plugin",
            "state": state,
            "device_id": None,
            "capabilities": capabilities,
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
        metadata = plugin.get("integration")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        integration_type = str(metadata.get("type") or plugin.get("id") or "plugin").replace(
            "-", "_"
        )
        state = _state_for_plugin(instance.get("state"))
        messages = int(instance.get("messages_received", 0))
        summary = str(
            instance.get("error")
            or instance.get("summary")
            or f"Connected · {messages} notifications"
        )
        details = {}
        if detailed:
            details = {
                **dict(instance.get("details") or {}),
                "messages_received": messages,
                "connected_at": instance.get("connected_at"),
                "last_message_at": instance.get("last_message_at"),
                "error": instance.get("error"),
            }
        capabilities = list(instance.get("capabilities") or [])
        capabilities = capabilities or list(metadata.get("capabilities") or [])
        return {
            "id": f"{integration_type}:{instance.get('id')}",
            "name": str(metadata.get("name") or plugin.get("name") or integration_type),
            "type": integration_type,
            "kind": "device",
            "state": state,
            "device_id": str(instance.get("id") or "") or None,
            "capabilities": capabilities,
            "summary": summary,
            "details": details,
        }
