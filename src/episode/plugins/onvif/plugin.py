from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import replace

from episode.domain.models import ReceiptStatus
from episode.ingestion.models import EventObservation, IngressHandlerResult, StoredIngressEnvelope
from episode.ingestion.router import IngressHandlerRegistration
from episode.plugins.models import (
    PluginContext,
    PluginInstanceState,
    PluginInstanceStatus,
    PluginState,
    PluginStatus,
    RawPluginDelivery,
)
from episode.plugins.onvif.device import ONVIFDeviceConnection, device_config
from episode.plugins.onvif.events import WSNT, ONVIFStateTracker, parse_notifications

logger = logging.getLogger(__name__)

PLUGIN_ID = "onvif"
PLUGIN_NAME = "ONVIF"
PLUGIN_KIND = "device-integration"
HANDLER_ID = "onvif-events"


def _configured_devices(devices: tuple[Mapping[str, object], ...]) -> list[Mapping[str, object]]:
    return [
        device
        for device in devices
        if "onvif" in device.get("capabilities", ()) and device.get("enabled", True)
    ]


class ONVIFPlugin:
    def __init__(self, context: PluginContext, *, connection_factory=ONVIFDeviceConnection):
        self._router = context.ingress_router
        self._delivery_sink = context.raw_delivery_sink
        self._media = context.media_registry
        self._device_update_sink = context.device_update_sink
        self._configured_devices = _configured_devices(context.configured_devices)
        self._connection_factory = connection_factory
        self._connections: list[ONVIFDeviceConnection] = []
        self._invalid_instances: list[PluginInstanceStatus] = []
        self._trackers: dict[str, ONVIFStateTracker] = {}
        self._event_counts: dict[str, int] = {}
        self._suppressed_counts: dict[str, int] = {}
        self._last_events: dict[str, str] = {}
        self._registered = False

    @staticmethod
    def _matches(envelope: StoredIngressEnvelope) -> bool:
        return envelope.transport == "plugin" and envelope.metadata.get("plugin_id") == PLUGIN_ID

    async def _handle(self, envelope: StoredIngressEnvelope) -> IngressHandlerResult:
        kind = envelope.metadata.get("kind")
        try:
            root = ET.fromstring(envelope.payload)
        except ET.ParseError:
            return IngressHandlerResult(
                claimed=True,
                status=ReceiptStatus.REJECTED,
                metadata={"reason": "invalid_onvif_xml"},
            )

        if kind == "pull_response":
            nodes = root.findall(f".//{{{WSNT}}}NotificationMessage")
            if self._delivery_sink is None:
                return IngressHandlerResult(
                    claimed=True,
                    status=ReceiptStatus.REJECTED,
                    metadata={"reason": "derived_delivery_storage_unavailable"},
                )
            for index, node in enumerate(nodes):
                await self._delivery_sink(
                    RawPluginDelivery(
                        plugin_id=PLUGIN_ID,
                        device_id=envelope.device_id,
                        area_id=envelope.area_id,
                        received_at=envelope.received_at,
                        payload=ET.tostring(node, encoding="utf-8", xml_declaration=True),
                        source="onvif:events",
                        media_type="application/xml",
                        artifact_type="derived_event_notification",
                        metadata={
                            "kind": "notification",
                            "integration": "onvif",
                            "parent_receipt_id": envelope.receipt_id,
                            "notification_index": index,
                        },
                    )
                )
            return IngressHandlerResult(
                claimed=True,
                status=ReceiptStatus.IGNORED,
                metadata={
                    "reason": "expanded_to_notifications" if nodes else "no_notifications",
                    "notification_count": len(nodes),
                },
            )

        if kind != "notification":
            return IngressHandlerResult(claimed=False)
        notifications = parse_notifications(root)
        if len(notifications) != 1:
            return IngressHandlerResult(
                claimed=True,
                status=ReceiptStatus.REJECTED,
                metadata={"reason": "invalid_notification_count"},
            )
        notification = notifications[0]
        tracker = self._trackers.setdefault(envelope.device_id, ONVIFStateTracker())
        if not tracker.is_transition(notification):
            reason = (
                "initial_or_unmapped"
                if notification.is_initial_value or not notification.event_type
                else "repeated_state"
            )
            if reason == "repeated_state":
                self._suppressed_counts[envelope.device_id] = (
                    self._suppressed_counts.get(envelope.device_id, 0) + 1
                )
            return IngressHandlerResult(
                claimed=True,
                status=ReceiptStatus.IGNORED,
                metadata={"reason": reason, "topic": notification.topic},
            )

        self._event_counts[envelope.device_id] = self._event_counts.get(envelope.device_id, 0) + 1
        self._last_events[envelope.device_id] = notification.timestamp.isoformat()
        return IngressHandlerResult(
            claimed=True,
            event=EventObservation(
                timestamp=notification.timestamp,
                event_type=notification.event_type or "",
                event_state=notification.event_state.value,
                source="onvif:events",
                device_id=envelope.device_id,
                area_id=envelope.area_id,
                metadata={
                    "onvif_topic": notification.topic,
                    "onvif_items": notification.items,
                },
            ),
            metadata={
                "interpreted": True,
                "topic": notification.topic,
                "property_operation": notification.property_operation,
            },
        )

    def status(self) -> PluginStatus:
        metrics = self._router.status(HANDLER_ID) if self._router else None
        instances = [*self._invalid_instances]
        for connection in self._connections:
            status = connection.status()
            details = {
                **dict(status.details),
                "events_received": self._event_counts.get(status.id, 0),
                "events_suppressed": self._suppressed_counts.get(status.id, 0),
                "last_event": self._last_events.get(status.id),
            }
            instances.append(replace(status, details=details))
        running = sum(item.state == PluginInstanceState.RUNNING for item in instances)
        if not self._registered or self._router is None:
            state = PluginState.FAILED
            error = "ONVIF ingress routing is unavailable."
        elif not instances or running == len(instances):
            state = PluginState.READY
            error = None
        elif running:
            state = PluginState.DEGRADED
            error = f"{len(instances) - running} ONVIF Device connection(s) unavailable."
        elif any(item.state == PluginInstanceState.STARTING for item in instances):
            state = PluginState.VALIDATING
            error = "ONVIF Device connections are starting or reconnecting."
        else:
            state = PluginState.FAILED
            error = "No configured ONVIF Device connections are available."
        if metrics and (metrics["failures"] or metrics["timeouts"]):
            state = PluginState.DEGRADED
            error = str(metrics["last_error"] or "ONVIF Event interpretation failed.")
        return PluginStatus(
            id=PLUGIN_ID,
            name=PLUGIN_NAME,
            kind=PLUGIN_KIND,
            state=state,
            error=error,
            instances=tuple(instances),
            metrics=metrics or {},
        )

    async def start(self) -> None:
        if self._router is None or self._registered:
            return
        self._router.register(
            IngressHandlerRegistration(id=HANDLER_ID, matcher=self._matches, handler=self._handle)
        )
        self._registered = True
        missing_service = None
        if self._delivery_sink is None:
            missing_service = "Raw plugin delivery storage is unavailable."
        elif self._media is None:
            missing_service = "Runtime media registration is unavailable."
        elif self._device_update_sink is None:
            missing_service = "Device discovery persistence is unavailable."
        if missing_service:
            self._invalid_instances = [
                PluginInstanceStatus(
                    id=str(device.get("id") or "unknown-device"),
                    name=str(device.get("name") or device.get("id") or "Unknown Device"),
                    state=PluginInstanceState.FAILED,
                    error=missing_service,
                )
                for device in self._configured_devices
            ]
            return

        for device in self._configured_devices:
            config, error = device_config(device)
            if config is None:
                self._invalid_instances.append(
                    PluginInstanceStatus(
                        id=str(device.get("id") or "unknown-device"),
                        name=str(device.get("name") or device.get("id") or "Unknown Device"),
                        state=PluginInstanceState.FAILED,
                        error=error,
                    )
                )
                continue
            self._connections.append(
                self._connection_factory(
                    config,
                    self._delivery_sink,
                    self._media,
                    self._device_update_sink,
                )
            )
        await asyncio.gather(*(connection.start() for connection in self._connections))

    async def stop(self) -> None:
        await asyncio.gather(
            *(connection.stop() for connection in reversed(self._connections)),
            return_exceptions=True,
        )
        self._connections.clear()
        if self._router is not None and self._registered:
            self._router.unregister(HANDLER_ID)
        self._registered = False
