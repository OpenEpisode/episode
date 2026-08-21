from __future__ import annotations

import logging

from episode.domain.models import ReceiptStatus
from episode.ingestion.models import (
    EventObservation,
    IngressHandlerResult,
    StoredIngressEnvelope,
)
from episode.ingestion.router import IngressHandlerRegistration
from episode.plugins.hikvision.xml_events import HikvisionEvent
from episode.plugins.models import PluginContext, PluginState, PluginStatus

logger = logging.getLogger(__name__)

PLUGIN_ID = "hikvision-alarm-server"
PLUGIN_NAME = "Hikvision Alarm Server"
PLUGIN_KIND = "ingress-handler"
HANDLER_ID = "hikvision-alarm-server-events"


def _extract_xml(payload: bytes) -> bytes | None:
    starts = [
        offset
        for marker in (b"<?xml", b"<EventNotificationAlert")
        if (offset := payload.find(marker)) >= 0
    ]
    if not starts:
        return None
    xml = payload[min(starts) :]
    boundary = xml.find(b"\r\n--")
    return xml[:boundary] if boundary >= 0 else xml


class HikvisionAlarmPlugin:
    def __init__(self, context: PluginContext):
        self._router = context.ingress_router
        self._registered = False

    @staticmethod
    def _matches(envelope: StoredIngressEnvelope) -> bool:
        return (
            envelope.transport == "http"
            and envelope.metadata.get("connector_type") == "alarm_server"
            and b"EventNotificationAlert" in envelope.payload
        )

    @staticmethod
    async def _handle(envelope: StoredIngressEnvelope) -> IngressHandlerResult:
        xml = _extract_xml(envelope.payload)
        if xml is None:
            return IngressHandlerResult(
                claimed=True,
                status=ReceiptStatus.REJECTED,
                metadata={"reason": "hikvision_xml_not_found"},
            )
        parsed = HikvisionEvent.from_bytes(xml)
        if parsed is None:
            logger.warning(
                "Hikvision Alarm Server received invalid XML in receipt %s",
                envelope.receipt_id,
            )
            return IngressHandlerResult(
                claimed=True,
                status=ReceiptStatus.REJECTED,
                metadata={"reason": "invalid_hikvision_xml"},
            )

        return IngressHandlerResult(
            claimed=True,
            event=EventObservation(
                timestamp=parsed.timestamp,
                event_type=parsed.event_type,
                event_state=parsed.event_state.value,
                source="hikvision:alarm_server",
                device_id=envelope.device_id,
                area_id=envelope.area_id,
                device_ip=parsed.ip_address,
                metadata=parsed.metadata,
            ),
            metadata={
                "vendor": "hikvision",
                "interpreted": True,
                "plugin_id": PLUGIN_ID,
            },
        )

    def status(self) -> PluginStatus:
        if self._router is None:
            return PluginStatus(
                id=PLUGIN_ID,
                name=PLUGIN_NAME,
                kind=PLUGIN_KIND,
                state=PluginState.FAILED,
                error="Ingress routing is unavailable.",
            )
        metrics = self._router.status(HANDLER_ID)
        state = PluginState.READY if self._registered else PluginState.VALIDATING
        error = None
        if metrics and (metrics["failures"] or metrics["timeouts"]):
            state = PluginState.DEGRADED
            error = str(metrics["last_error"] or "Alarm handler failed.")
        return PluginStatus(
            id=PLUGIN_ID,
            name=PLUGIN_NAME,
            kind=PLUGIN_KIND,
            state=state,
            error=error,
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

    async def stop(self) -> None:
        if self._router is not None and self._registered:
            self._router.unregister(HANDLER_ID)
        self._registered = False
