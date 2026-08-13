from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from episode.domain.models import EventState

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
    """Decode one vendor EventNotificationAlert without owning persistence."""

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
        value = self._root.findtext(".//hk:dateTime", "", _HK_NS)
        try:
            return datetime.fromisoformat(value)
        except (ValueError, TypeError):
            return datetime.now(tz=timezone.utc)

    @property
    def event_state(self) -> EventState:
        value = self._root.findtext(".//hk:eventState", "active", _HK_NS)
        return EventState.ACTIVE if value == "active" else EventState.INACTIVE

    @property
    def vendor_event_type(self) -> str:
        return self._root.findtext(".//hk:eventType", "", _HK_NS)

    @property
    def event_type(self) -> str:
        target = self._root.findtext(".//hk:targetType", "", _HK_NS)
        mapped = _TARGET_TYPE_MAP.get(target.lower())
        if mapped:
            return mapped
        vendor_type = self.vendor_event_type
        return _EVENT_TYPE_MAP.get(vendor_type, vendor_type.lower())

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
