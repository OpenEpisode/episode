from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from episode.domain.models import ReceiptStatus
from episode.ingestion.models import (
    EvidenceObservation,
    IngressHandlerResult,
    StoredIngressEnvelope,
)
from episode.ingestion.router import IngressHandlerRegistration
from episode.plugins.models import PluginContext, PluginState, PluginStatus

logger = logging.getLogger(__name__)

PLUGIN_ID = "hikvision-ftp"
PLUGIN_NAME = "Hikvision FTP snapshots"
PLUGIN_KIND = "file-ingress-handler"
HANDLER_ID = "hikvision-ftp-snapshots"

_FILENAME_PATTERNS = [
    re.compile(
        r"(?P<ts>\d{14})(?P<ms>\d{3})?_(?P<video_intercom_event>[^_]+)_(?P<ip>(?:\d{1,3}\.){3}\d{1,3})\.jpg$"
    ),
    re.compile(
        r"(?P<ip>[0-9.]+)_(?P<channel>[A-Za-z0-9]+)_(?P<ts>\d{14})(?P<ms>\d{3})?_(?P<event>.+)\.jpg$"
    ),
    re.compile(r"_(?P<ts>\d{14})(?P<ms>\d{3})?_(?P<event>.+)\.jpg$"),
    re.compile(
        r"--(?P<ipv6>[0-9a-f:-]+)_(?P<channel>[A-Za-z0-9]+)_(?P<ts>\d{14})(?P<ms>\d{3})?_(?P<event>.+)\.jpg$",
    ),
]


def parse_hikvision_filename(filename: str) -> dict[str, object]:
    for pattern in _FILENAME_PATTERNS:
        match = pattern.match(filename)
        if not match:
            continue
        groups = match.groupdict()
        result: dict[str, object] = {}
        if groups.get("ip"):
            result["ip_address"] = groups["ip"]
        if groups.get("ipv6"):
            result["ip_address"] = groups["ipv6"]
        ts_str = groups.get("ts", "")
        ms_str = groups.get("ms", "")
        if ts_str:
            try:
                base = datetime.strptime(ts_str, "%Y%m%d%H%M%S")
                milliseconds = int(ms_str) if ms_str else 0
                result["timestamp"] = base.replace(
                    tzinfo=timezone(timedelta(hours=1)),
                    microsecond=milliseconds * 1000,
                )
            except ValueError:
                pass
        event_type = groups.get("event") or groups.get("video_intercom_event")
        if event_type:
            result["event_type"] = event_type.lower()
        if groups.get("video_intercom_event"):
            result["filename_profile"] = "video_intercom"
        return result
    return {}


class HikvisionFTPPlugin:
    def __init__(self, context: PluginContext):
        self._router = context.ingress_router
        self._devices_by_ip = {
            str(device.get("ip_address")): device
            for device in context.configured_devices
            if device.get("ip_address")
        }
        self._registered = False

    @staticmethod
    def _matches(envelope: StoredIngressEnvelope) -> bool:
        filename = envelope.original_filename or ""
        return (
            envelope.transport == "ftp"
            and envelope.metadata.get("connector_type") == "ftp"
            and Path(filename).suffix.lower() in {".jpg", ".jpeg"}
            and bool(parse_hikvision_filename(filename))
        )

    async def _handle(self, envelope: StoredIngressEnvelope) -> IngressHandlerResult:
        filename = envelope.original_filename or ""
        parsed = parse_hikvision_filename(filename)
        if not parsed:
            return IngressHandlerResult(claimed=False)

        timestamp = parsed.pop("timestamp", envelope.received_at)
        ip_address = str(parsed.get("ip_address", ""))
        device = self._devices_by_ip.get(ip_address)
        device_type = str(device.get("device_type", "")) if device else ""
        metadata = {**parsed, "origin": "ftp"}
        if device_type == "doorbell":
            metadata["evidence_role"] = "event_attachment"
            metadata["timelapse_eligible"] = False

        return IngressHandlerResult(
            claimed=True,
            status=ReceiptStatus.ACCEPTED,
            evidence=EvidenceObservation(
                timestamp=timestamp,
                evidence_type="snapshot",
                source="hikvision:ftp",
                mime_type="image/jpeg",
                device_ip=ip_address,
                original_filename=filename,
                metadata=metadata,
            ),
            metadata={"vendor": "hikvision", "interpreted": True},
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
            error = str(metrics["last_error"] or "FTP snapshot handler failed.")
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
