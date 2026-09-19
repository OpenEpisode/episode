from __future__ import annotations

from collections.abc import Awaitable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Protocol

from episode.domain.models import Device

if TYPE_CHECKING:
    from episode.ingestion.router import IngressHandlerRegistration
    from episode.media.registry import CameraMedia


class PluginState(StrEnum):
    NOT_INSTALLED = "not_installed"
    INCOMPLETE = "incomplete"
    INCOMPATIBLE = "incompatible"
    VALIDATING = "validating"
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"


class PluginInstanceState(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass(frozen=True)
class PluginDeviceInfo:
    manufacturer: str | None = None
    model: str | None = None
    firmware_version: str | None = None


@dataclass(frozen=True)
class PluginInstanceStatus:
    id: str
    name: str
    state: PluginInstanceState
    messages_received: int = 0
    connected_at: datetime | None = None
    last_message_at: datetime | None = None
    error: str | None = None
    device_info: PluginDeviceInfo | None = None
    summary: str | None = None
    capabilities: tuple[str, ...] = ()
    details: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class PluginStatus:
    id: str
    name: str
    kind: str
    state: PluginState
    version: str | None = None
    architecture: str | None = None
    error: str | None = None
    summary: str | None = None
    instances: tuple[PluginInstanceStatus, ...] = ()
    metrics: Mapping[str, object] = field(default_factory=dict)

    def public(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class PluginEvent:
    timestamp: datetime
    event_type: str
    event_state: str
    source: str
    dedup_key: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class RawPluginDelivery:
    plugin_id: str
    device_id: str
    area_id: str
    received_at: datetime
    payload: bytes
    metadata: Mapping[str, object]
    source: str = ""
    media_type: str = "application/octet-stream"
    artifact_type: str = "plugin_notification"


RawPluginDeliverySink = Callable[[RawPluginDelivery], Awaitable[None]]
PluginDeviceUpdateSink = Callable[[Device], Awaitable[Device]]


class PluginIngressRouter(Protocol):
    """Narrow ingress service exposed to plugins."""

    def register(self, registration: IngressHandlerRegistration) -> None: ...

    def unregister(self, handler_id: str) -> None: ...

    def status(self, handler_id: str) -> Mapping[str, object] | None: ...


class PluginMediaRegistry(Protocol):
    """Narrow media discovery service exposed to plugins."""

    def register(self, source: CameraMedia) -> None: ...

    def get(self, device_id: str) -> CameraMedia | None: ...

    def unregister(self, device_id: str, *, source: str | None = None) -> None: ...


@dataclass(frozen=True)
class PluginContext:
    plugins_dir: Path
    configured_devices: tuple[Mapping[str, object], ...] = ()
    raw_delivery_sink: RawPluginDeliverySink | None = None
    ingress_router: PluginIngressRouter | None = None
    media_registry: PluginMediaRegistry | None = None
    device_update_sink: PluginDeviceUpdateSink | None = None


class ManagedPlugin(Protocol):
    def status(self) -> PluginStatus: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


PluginFactory = Callable[[PluginContext], ManagedPlugin]
PluginDeviceValidator = Callable[[object, str, float], Awaitable[Mapping[str, object]]]


@dataclass(frozen=True)
class PluginIntegration:
    """Operational metadata consumed by the core without loading plugin code."""

    type: str
    name: str
    device_scoped: bool = False
    capabilities: tuple[str, ...] = ()
    # ``unspecified`` is deliberately not treated as universal.  It is the
    # safe value for older third-party manifests whose targeting metadata is
    # not known yet.
    manufacturer_scope: tuple[str, ...] = ()
    manufacturer_scope_kind: str = "unspecified"
    device_types: tuple[str, ...] = ()

    def matches_device(self, *, manufacturer: str | None, device_type: str) -> bool:
        """Return whether this integration is a safe onboarding candidate.

        This is catalogue metadata only; it never imports or probes plugin
        code.  A targeted plugin requires a discovered manufacturer, while an
        explicitly universal plugin can be offered without one.
        """
        if self.device_types and device_type not in self.device_types:
            return False
        if self.manufacturer_scope_kind == "universal":
            return True
        if self.manufacturer_scope_kind != "targeted" or not manufacturer:
            return False
        normalized = normalize_manufacturer(manufacturer)
        return normalized in {normalize_manufacturer(value) for value in self.manufacturer_scope}


def normalize_manufacturer(value: str) -> str:
    """Normalize a discovered manufacturer for matching, not display."""
    normalized = "".join(character for character in value.casefold() if character.isalnum())
    # Keep aliases narrow and explicit: this is advisory onboarding metadata,
    # not a general-purpose fuzzy matcher.
    if normalized.startswith("hikvision"):
        return "hikvision"
    if normalized.startswith("reolink"):
        return "reolink"
    return normalized


@dataclass(frozen=True)
class PluginRegistration:
    id: str
    name: str
    kind: str
    activation_config_type: str
    factory: PluginFactory
    activation_connector_type: str = ""
    validation_capability: str = ""
    validator: PluginDeviceValidator | None = None
    integration: PluginIntegration | None = None
    explicitly_enabled: bool = False
    configured_device_ids: tuple[str, ...] = ()
    installed_version: str | None = None
    unavailable_state: PluginState | None = None
    unavailable_error: str | None = None

    def validating_status(self) -> PluginStatus:
        return PluginStatus(
            id=self.id,
            name=self.name,
            kind=self.kind,
            state=self.unavailable_state or PluginState.VALIDATING,
            version=self.installed_version,
            error=self.unavailable_error,
        )

    def public_catalog_entry(self) -> dict[str, object] | None:
        """Return bounded, non-secret catalogue data for device onboarding."""
        integration = self.integration
        if integration is None or not integration.device_scoped:
            return None
        return {
            "id": self.id,
            "name": integration.name,
            "type": integration.type,
            "kind": self.kind,
            "capabilities": list(integration.capabilities),
            "manufacturer_scope": list(integration.manufacturer_scope),
            "manufacturer_scope_kind": integration.manufacturer_scope_kind,
            "device_types": list(integration.device_types),
            "configured": bool(self.explicitly_enabled or self.configured_device_ids),
            "available": self.unavailable_state is None,
            "selection_required": True,
        }
