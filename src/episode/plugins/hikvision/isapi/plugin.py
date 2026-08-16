from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping

from episode.domain.models import ReceiptStatus
from episode.ingestion.models import (
    EventObservation,
    IngressHandlerResult,
    StoredIngressEnvelope,
)
from episode.ingestion.router import IngressHandlerRegistration
from episode.plugins.hikvision.isapi.runtime import ISAPIDeviceConnection, device_config
from episode.plugins.hikvision.xml_events import HikvisionEvent
from episode.plugins.models import (
    PluginContext,
    PluginInstanceState,
    PluginInstanceStatus,
    PluginState,
    PluginStatus,
)

logger = logging.getLogger(__name__)

PLUGIN_ID = "hikvision-isapi"
PLUGIN_NAME = "Hikvision ISAPI"
PLUGIN_KIND = "device-integration"
HANDLER_ID = "hikvision-isapi-events"


def _configured_devices(devices: tuple[Mapping[str, object], ...]) -> list[Mapping[str, object]]:
    return [
        device
        for device in devices
        if "isapi" in device.get("capabilities", ()) and device.get("enabled", True)
    ]


class HikvisionISAPIPlugin:
    def __init__(self, context: PluginContext, *, connection_factory=ISAPIDeviceConnection):
        self._router = context.ingress_router
        self._delivery_sink = context.raw_delivery_sink
        self._configured_devices = _configured_devices(context.configured_devices)
        self._connection_factory = connection_factory
        self._connections: list[ISAPIDeviceConnection] = []
        self._invalid_instances: list[PluginInstanceStatus] = []
        self._registered = False

    @staticmethod
    def _matches(envelope: StoredIngressEnvelope) -> bool:
        return envelope.transport == "plugin" and envelope.metadata.get("plugin_id") == PLUGIN_ID

    @staticmethod
    async def _handle(envelope: StoredIngressEnvelope) -> IngressHandlerResult:
        parsed = HikvisionEvent.from_bytes(envelope.payload)
        if parsed is None:
            logger.warning("Invalid Hikvision ISAPI XML in receipt %s", envelope.receipt_id)
            return IngressHandlerResult(
                claimed=True,
                status=ReceiptStatus.REJECTED,
                metadata={"vendor": "hikvision", "reason": "invalid_hikvision_xml"},
            )

        ignored = envelope.metadata.get("ignore_events", ())
        ignored_names = (
            {str(value).strip().lower() for value in ignored}
            if isinstance(ignored, (list, tuple))
            else set()
        )
        if {
            parsed.event_type.lower(),
            parsed.vendor_event_type.lower(),
        } & ignored_names:
            return IngressHandlerResult(
                claimed=True,
                status=ReceiptStatus.IGNORED,
                metadata={
                    "vendor": "hikvision",
                    "interpreted": True,
                    "ignored_event_type": parsed.event_type,
                },
            )

        return IngressHandlerResult(
            claimed=True,
            event=EventObservation(
                timestamp=parsed.timestamp,
                event_type=parsed.event_type,
                event_state=parsed.event_state.value,
                source="hikvision:isapi",
                device_id=envelope.device_id,
                area_id=envelope.area_id,
                device_ip=parsed.ip_address,
                metadata=parsed.metadata,
            ),
            metadata={"vendor": "hikvision", "interpreted": True},
        )

    def status(self) -> PluginStatus:
        metrics = self._router.status(HANDLER_ID) if self._router else None
        instances = (
            *self._invalid_instances,
            *(connection.status() for connection in self._connections),
        )
        running = sum(item.state == PluginInstanceState.RUNNING for item in instances)
        unavailable = len(instances) - running
        if not self._registered or self._router is None:
            state = PluginState.FAILED
            error = "ISAPI ingress routing is unavailable."
        elif not instances:
            state = PluginState.READY
            error = None
        elif unavailable == 0:
            state = PluginState.READY
            error = None
        elif running:
            state = PluginState.DEGRADED
            error = f"{unavailable} ISAPI device connection(s) unavailable."
        elif any(item.state == PluginInstanceState.STARTING for item in instances):
            state = PluginState.VALIDATING
            error = "ISAPI device connections are starting or reconnecting."
        else:
            state = PluginState.FAILED
            error = "No configured ISAPI device connections are available."
        if metrics and (metrics["failures"] or metrics["timeouts"]):
            state = PluginState.DEGRADED
            error = str(metrics["last_error"] or "ISAPI event interpretation failed.")
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
            IngressHandlerRegistration(
                id=HANDLER_ID,
                matcher=self._matches,
                handler=self._handle,
            )
        )
        self._registered = True
        if self._delivery_sink is None:
            self._invalid_instances = [
                PluginInstanceStatus(
                    id=str(device.get("id") or "unknown-device"),
                    name=str(device.get("name") or device.get("id") or "Unknown device"),
                    state=PluginInstanceState.FAILED,
                    error="Raw plugin delivery storage is unavailable.",
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
                        name=str(device.get("name") or device.get("id") or "Unknown device"),
                        state=PluginInstanceState.FAILED,
                        error=error,
                    )
                )
                continue
            self._connections.append(self._connection_factory(config, self._delivery_sink))
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
