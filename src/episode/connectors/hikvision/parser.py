from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from episode.domain.models import (
    EventState,
    IngestionReceipt,
    RawArtifact,
    ReceiptStatus,
)
from episode.storage.files import describe_artifact, save_payload

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_EVENT_TYPE_MAP = {
    "VMD": "motion_detection",
    "videoloss": "video_loss",
}

_TARGET_TYPE_MAP = {
    "human": "human_detection",
    "vehicle": "vehicle_detection",
}

_HK_NS = {"hk": "http://www.hikvision.com/ver20/XMLSchema"}


class HikvisionEvent:
    def __init__(self, xml_text: str):
        self._root = ET.fromstring(xml_text)

    @classmethod
    def from_bytes(cls, data: bytes) -> HikvisionEvent | None:
        try:
            return cls(data.decode("utf-8", errors="replace"))
        except ET.ParseError:
            return None

    @property
    def timestamp(self) -> datetime:
        dt = self._root.findtext(".//hk:dateTime", "", _HK_NS)
        try:
            return datetime.fromisoformat(dt)
        except (ValueError, TypeError):
            return datetime.now(tz=timezone.utc)

    @property
    def event_state(self) -> EventState:
        s = self._root.findtext(".//hk:eventState", "active", _HK_NS)
        return EventState.ACTIVE if s == "active" else EventState.INACTIVE

    @property
    def event_type(self) -> str:
        ev_type = self._root.findtext(".//hk:eventType", "", _HK_NS)
        target = self._root.findtext(".//hk:targetType", "", _HK_NS)
        mapped = _TARGET_TYPE_MAP.get(target.lower())
        if mapped:
            return mapped
        return _EVENT_TYPE_MAP.get(ev_type, ev_type.lower())

    @property
    def channel_name(self) -> str:
        return self._root.findtext(".//hk:channelName", "", _HK_NS)

    @property
    def ip_address(self) -> str:
        return self._root.findtext(".//hk:ipAddress", "", _HK_NS)

    @property
    def target_type(self) -> str | None:
        value = self._root.findtext(".//hk:targetType", "", _HK_NS).strip()
        return value or None

    @property
    def bounding_box(self) -> dict[str, float] | None:
        rect = self._root.find(".//hk:targetRect", _HK_NS)
        if rect is None:
            return None
        try:
            return {
                "x": float(rect.findtext("hk:X", "0", _HK_NS)),
                "y": float(rect.findtext("hk:Y", "0", _HK_NS)),
                "width": float(rect.findtext("hk:width", "0", _HK_NS)),
                "height": float(rect.findtext("hk:height", "0", _HK_NS)),
            }
        except (TypeError, ValueError):
            return None

    @property
    def metadata(self) -> dict[str, object]:
        metadata: dict[str, object] = {"vendor": "hikvision"}
        if self.channel_name:
            metadata["channel_name"] = self.channel_name
        if self.target_type:
            metadata["target_type"] = self.target_type
        if self.bounding_box:
            metadata["bounding_box"] = self.bounding_box
        return metadata

    @property
    def raw_xml(self) -> str:
        return ET.tostring(self._root, encoding="unicode")

    def to_event_dict(
        self,
        device_id: str,
        area_id: str,
        source: str,
        payload_path: str | None = None,
    ) -> dict:
        return {
            "device_id": device_id,
            "area_id": area_id,
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "event_state": self.event_state.value,
            "source": source,
            "raw_payload_path": payload_path,
            "metadata": self.metadata,
        }


@dataclass
class HikvisionDelivery:
    event: dict | None
    artifact: RawArtifact
    receipt: IngestionReceipt


def ingest_hikvision_xml(
    xml_data: bytes,
    device_id: str,
    area_id: str,
    source: str,
    events_root: str,
) -> HikvisionDelivery:
    prefix = "isapi" if "isapi" in source else "alarm"
    payload_path = save_payload(events_root, "events", xml_data, prefix=prefix)
    artifact = describe_artifact(
        payload_path,
        "event_payload",
        "application/xml",
        metadata={"vendor": "hikvision"},
    )

    parsed = HikvisionEvent.from_bytes(xml_data)
    status = ReceiptStatus.ACCEPTED
    metadata: dict = {"vendor": "hikvision"}
    if parsed is None:
        status = ReceiptStatus.REJECTED
        metadata["reason"] = "invalid_xml"
        preview = xml_data[:200]
        logger.warning(
            "Failed to parse XML from %s device=%s (%d bytes, preview=%s)",
            source,
            device_id,
            len(xml_data),
            preview,
        )

    receipt = IngestionReceipt(
        source=source,
        observed_at=parsed.timestamp if parsed else None,
        status=status,
        artifact_id=artifact.id,
        device_id=device_id,
        area_id=area_id,
        metadata=metadata,
    )
    event = parsed.to_event_dict(device_id, area_id, source, payload_path) if parsed else None
    return HikvisionDelivery(event=event, artifact=artifact, receipt=receipt)


def parse_hikvision_xml(
    xml_data: bytes,
    device_id: str,
    area_id: str,
    source: str,
    events_root: str,
) -> dict | None:
    """Compatibility wrapper for callers that only need the normalized event."""
    return ingest_hikvision_xml(xml_data, device_id, area_id, source, events_root).event
