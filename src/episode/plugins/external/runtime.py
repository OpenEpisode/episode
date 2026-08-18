from __future__ import annotations

import importlib.util
import re
import sys
from collections.abc import Mapping
from dataclasses import asdict
from hashlib import sha256

from episode import plugin_api
from episode.config import ExternalPluginConfig
from episode.domain.models import ReceiptStatus as CoreReceiptStatus
from episode.ingestion import models as ingress_models
from episode.ingestion.router import IngressHandlerRegistration
from episode.media import CameraMedia
from episode.plugins.external.manifest import ExternalPluginManifest
from episode.plugins.models import (
    ManagedPlugin,
    PluginContext,
    PluginInstanceState,
    PluginInstanceStatus,
    PluginState,
    PluginStatus,
    RawPluginDelivery,
)


def _load_entrypoint(manifest: ExternalPluginManifest):
    if not manifest.entrypoint_file.is_file():
        raise plugin_api.PluginConfigurationError(
            f"Plugin entrypoint {manifest.entrypoint_file.name!r} does not exist."
        )
    digest = sha256(str(manifest.root.resolve()).encode()).hexdigest()[:12]
    safe_id = re.sub(r"[^a-z0-9_]", "_", manifest.id)
    module_name = f"_episode_external_{safe_id}_{digest}"
    package = manifest.entrypoint_file.name == "__init__.py"
    spec = importlib.util.spec_from_file_location(
        module_name,
        manifest.entrypoint_file,
        submodule_search_locations=[str(manifest.entrypoint_file.parent)] if package else None,
    )
    if spec is None or spec.loader is None:
        raise plugin_api.PluginConfigurationError("Plugin entrypoint could not be loaded.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    factory = getattr(module, manifest.entrypoint_symbol, None)
    if not callable(factory):
        raise plugin_api.PluginConfigurationError(
            f"Plugin entrypoint symbol {manifest.entrypoint_symbol!r} is not callable."
        )
    return factory


def _stored_delivery(envelope: ingress_models.StoredIngressEnvelope) -> plugin_api.StoredDelivery:
    return plugin_api.StoredDelivery(
        receipt_id=envelope.receipt_id,
        artifact_id=envelope.artifact_id,
        received_at=envelope.received_at,
        payload=envelope.payload,
        media_type=envelope.media_type,
        byte_size=envelope.byte_size,
        sha256=envelope.sha256,
        device_id=envelope.device_id,
        area_id=envelope.area_id,
        source=envelope.source,
        transport=envelope.transport,
        original_filename=envelope.original_filename,
        metadata=envelope.metadata,
    )


class _ExternalIngress:
    def __init__(
        self,
        plugin_id: str,
        plugin_kind: str,
        context: PluginContext,
        devices: tuple[plugin_api.DeviceConfig, ...],
    ) -> None:
        if context.ingress_router is None or context.raw_delivery_sink is None:
            raise plugin_api.PluginConfigurationError("Raw-first ingress services are unavailable.")
        self._plugin_id = plugin_id
        self._plugin_kind = plugin_kind
        self._router = context.ingress_router
        self._sink = context.raw_delivery_sink
        self._devices = {device.id: device for device in devices}
        self._handlers: dict[str, str] = {}

    def register(self, registration: plugin_api.HandlerRegistration) -> None:
        if registration.id in self._handlers:
            raise ValueError(f"Handler {registration.id!r} is already registered")
        internal_id = f"external:{self._plugin_id}:{registration.id}"

        def matches(envelope: ingress_models.StoredIngressEnvelope) -> bool:
            belongs_to_plugin = (
                envelope.transport == "plugin"
                and envelope.metadata.get("plugin_id") == self._plugin_id
            )
            eligible = (
                belongs_to_plugin
                if self._plugin_kind == "device"
                else envelope.transport != "plugin"
            )
            return eligible and registration.matcher(_stored_delivery(envelope))

        async def handle(
            envelope: ingress_models.StoredIngressEnvelope,
        ) -> ingress_models.IngressHandlerResult:
            result = await registration.handler(_stored_delivery(envelope))
            if not isinstance(result, plugin_api.HandlerResult):
                raise TypeError("Plugin handler must return episode.plugin_api.HandlerResult")
            event = result.event
            evidence = result.evidence
            return ingress_models.IngressHandlerResult(
                claimed=result.claimed,
                status=CoreReceiptStatus(result.status.value),
                event=(
                    ingress_models.EventObservation(
                        timestamp=event.timestamp,
                        event_type=event.event_type,
                        event_state=event.event_state.value,
                        source=event.source or f"plugin:{self._plugin_id}",
                        device_id=(
                            envelope.device_id if self._plugin_kind == "device" else event.device_id
                        ),
                        area_id=envelope.area_id if self._plugin_kind == "device" else "",
                        device_ip=event.device_address,
                        dedup_key=event.dedup_key,
                        metadata=event.metadata,
                    )
                    if event
                    else None
                ),
                evidence=(
                    ingress_models.EvidenceObservation(
                        timestamp=evidence.timestamp,
                        evidence_type=evidence.evidence_type,
                        source=evidence.source or f"plugin:{self._plugin_id}",
                        mime_type=evidence.mime_type,
                        device_id=(
                            envelope.device_id
                            if self._plugin_kind == "device"
                            else evidence.device_id
                        ),
                        area_id=envelope.area_id if self._plugin_kind == "device" else "",
                        device_ip=evidence.device_address,
                        original_filename=evidence.original_filename,
                        metadata=evidence.metadata,
                    )
                    if evidence
                    else None
                ),
                external_id=result.external_id,
                metadata={"external_plugin": self._plugin_id, **dict(result.metadata)},
            )

        self._router.register(
            IngressHandlerRegistration(
                id=internal_id,
                matcher=matches,
                handler=handle,
                timeout=registration.timeout,
            )
        )
        self._handlers[registration.id] = internal_id

    def unregister(self, handler_id: str) -> None:
        internal_id = self._handlers.pop(handler_id, None)
        if internal_id:
            self._router.unregister(internal_id)

    async def submit(self, delivery: plugin_api.RawDelivery) -> None:
        device = self._devices.get(delivery.device_id)
        if device is None:
            raise ValueError(
                f"Device {delivery.device_id!r} is not assigned to plugin {self._plugin_id!r}"
            )
        await self._sink(
            RawPluginDelivery(
                plugin_id=self._plugin_id,
                device_id=device.id,
                area_id=device.area_id,
                received_at=delivery.received_at,
                payload=delivery.payload,
                source=delivery.source or f"plugin:{self._plugin_id}",
                media_type=delivery.media_type,
                artifact_type=delivery.artifact_type,
                metadata=dict(delivery.metadata),
            )
        )

    def status(self, handler_id: str) -> Mapping[str, object] | None:
        internal_id = self._handlers.get(handler_id)
        return self._router.status(internal_id) if internal_id else None

    def close(self) -> None:
        for handler_id in tuple(self._handlers):
            self.unregister(handler_id)


class _ExternalMedia:
    def __init__(
        self,
        plugin_id: str,
        context: PluginContext,
        devices: tuple[plugin_api.DeviceConfig, ...],
    ) -> None:
        self._plugin_id = plugin_id
        self._registry = context.media_registry
        self._device_ids = {device.id for device in devices}
        self._registered: set[str] = set()

    def register(self, source: plugin_api.MediaSource) -> None:
        if source.device_id not in self._device_ids:
            raise ValueError(
                f"Device {source.device_id!r} is not assigned to plugin {self._plugin_id!r}"
            )
        if self._registry is None:
            raise plugin_api.PluginConfigurationError("Runtime media registration is unavailable.")
        source_name = f"external:{self._plugin_id}"
        self._registry.register(
            CameraMedia(
                device_id=source.device_id,
                stream_uri=source.stream_uri,
                snapshot_uri=source.snapshot_uri,
                username=source.username,
                password=source.password,
                profile_token=source.profile_token,
                source=source_name,
            )
        )
        self._registered.add(source.device_id)

    def unregister(self, device_id: str) -> None:
        if device_id not in self._registered:
            return
        if self._registry is not None:
            self._registry.unregister(device_id, source=f"external:{self._plugin_id}")
        self._registered.discard(device_id)

    def close(self) -> None:
        for device_id in tuple(self._registered):
            self.unregister(device_id)


def _public_device(value: Mapping[str, object], plugin_id: str) -> plugin_api.DeviceConfig:
    configs = value.get("configs", {})
    plugin_configuration: Mapping[str, object] = {}
    if isinstance(configs, Mapping):
        selected = configs.get(plugin_id, {})
        if hasattr(selected, "__dataclass_fields__"):
            plugin_configuration = asdict(selected)
        elif isinstance(selected, Mapping):
            plugin_configuration = selected
    return plugin_api.DeviceConfig(
        id=str(value.get("id", "")),
        name=str(value.get("name", "")),
        device_type=str(value.get("device_type", "")),
        area_id=str(value.get("area_id", "")),
        address=str(value.get("ip_address", "")),
        username=str(value.get("username", "")),
        password=str(value.get("password", "")),
        configuration=plugin_configuration,
    )


def _instance_status(value: plugin_api.InstanceStatus) -> PluginInstanceStatus:
    return PluginInstanceStatus(
        id=value.id,
        name=value.name,
        state=PluginInstanceState(value.state.value),
        messages_received=value.messages_received,
        connected_at=value.connected_at,
        last_message_at=value.last_message_at,
        error=value.error,
        summary=value.summary,
        capabilities=value.capabilities,
        details=dict(value.details),
    )


class ExternalManagedPlugin(ManagedPlugin):
    def __init__(
        self,
        manifest: ExternalPluginManifest,
        configured: ExternalPluginConfig,
        context: PluginContext,
    ) -> None:
        available = {
            str(device.get("id")): device
            for device in context.configured_devices
            if device.get("enabled", True)
        }
        missing = [device_id for device_id in configured.device_ids if device_id not in available]
        if missing:
            raise plugin_api.PluginConfigurationError(
                "Configured Device IDs are missing or disabled: " + ", ".join(missing)
            )
        devices = tuple(
            _public_device(available[device_id], manifest.id) for device_id in configured.device_ids
        )
        if manifest.kind == "device" and not devices:
            raise plugin_api.PluginConfigurationError(
                "A device plugin requires at least one device_id."
            )
        self._manifest = manifest
        self._ingress = _ExternalIngress(manifest.id, manifest.kind, context, devices)
        self._media = _ExternalMedia(manifest.id, context, devices)
        factory = _load_entrypoint(manifest)
        self._plugin = factory(
            plugin_api.PluginContext(
                plugin_id=manifest.id,
                plugin_dir=manifest.root,
                settings=configured.settings,
                devices=devices,
                ingress=self._ingress,
                media=self._media,
            )
        )
        for method in ("status", "start", "stop"):
            if not callable(getattr(self._plugin, method, None)):
                raise plugin_api.PluginConfigurationError(
                    f"Plugin does not implement required {method}()."
                )

    def status(self) -> PluginStatus:
        value = self._plugin.status()
        if not isinstance(value, plugin_api.PluginStatus):
            raise TypeError("Plugin status() must return episode.plugin_api.PluginStatus")
        return PluginStatus(
            id=self._manifest.id,
            name=self._manifest.name,
            kind=self._manifest.kind,
            state=PluginState(value.state.value),
            version=self._manifest.version,
            error=value.error,
            summary=value.summary,
            instances=tuple(_instance_status(instance) for instance in value.instances),
            metrics=dict(value.metrics),
        )

    async def start(self) -> None:
        await self._plugin.start()

    async def stop(self) -> None:
        try:
            await self._plugin.stop()
        finally:
            self._ingress.close()
            self._media.close()
