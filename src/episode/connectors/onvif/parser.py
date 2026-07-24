from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone

from episode.domain.models import EventState

TT = "http://www.onvif.org/ver10/schema"
WSNT = "http://docs.oasis-open.org/wsn/b-2"


@dataclass(frozen=True)
class ONVIFNotification:
    topic: str
    timestamp: datetime
    property_operation: str
    items: dict[str, str] = field(default_factory=dict)
    event_type: str | None = None
    event_state: EventState = EventState.ACTIVE

    @property
    def is_initial_value(self) -> bool:
        return self.property_operation.lower() == "initialized"


def _event_type(topic: str, items: dict[str, str]) -> str | None:
    text = " ".join((topic, *items.keys(), *items.values())).lower()
    if any(value in text for value in ("human", "person", "people")):
        return "human_detection"
    if "vehicle" in text:
        return "vehicle_detection"
    if "tamper" in text:
        return "tamper_detection"
    if "motion" in text:
        return "motion_detection"
    if "audio" in text and "alarm" in text:
        return "audio_detection"
    if "digitalinput" in text or "digital input" in text:
        return "digital_input"
    return None


def _event_state(items: dict[str, str]) -> EventState:
    preferred = (
        "IsMotion",
        "IsTamper",
        "State",
        "LogicalState",
        "Active",
        "IsInside",
    )
    value = next((items[name] for name in preferred if name in items), "true")
    return (
        EventState.INACTIVE
        if value.strip().lower() in {"false", "0", "off", "inactive", "idle"}
        else EventState.ACTIVE
    )


def _timestamp(value: str) -> datetime:
    if value.endswith("Z"):
        value = f"{value[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return datetime.now(timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_notifications(root: ET.Element) -> list[ONVIFNotification]:
    notifications: list[ONVIFNotification] = []
    for node in root.findall(f".//{{{WSNT}}}NotificationMessage"):
        topic = node.findtext(f"{{{WSNT}}}Topic", "").strip()
        message = node.find(f".//{{{TT}}}Message")
        if message is None:
            continue
        items = {
            item.attrib.get("Name", ""): item.attrib.get("Value", "")
            for item in message.findall(f".//{{{TT}}}SimpleItem")
            if item.attrib.get("Name")
        }
        notifications.append(
            ONVIFNotification(
                topic=topic,
                timestamp=_timestamp(message.attrib.get("UtcTime", "")),
                property_operation=message.attrib.get("PropertyOperation", ""),
                items=items,
                event_type=_event_type(topic, items),
                event_state=_event_state(items),
            )
        )
    return notifications


class ONVIFStateTracker:
    """Aggregate level-style ONVIF topics into semantic device transitions."""

    def __init__(self):
        self._topic_states: dict[tuple[str, str, str, str], EventState] = {}
        self._semantic_states: dict[str, EventState] = {}

    @staticmethod
    def _key(notification: ONVIFNotification) -> tuple[str, str, str, str]:
        source = (
            notification.items.get("VideoSourceConfigurationToken")
            or notification.items.get("Source")
            or ""
        )
        rule = notification.items.get("Rule", "")
        return notification.event_type or "", notification.topic, source, rule

    def is_transition(self, notification: ONVIFNotification) -> bool:
        event_type = notification.event_type
        if not event_type:
            return False

        self._topic_states[self._key(notification)] = notification.event_state
        previous = self._semantic_states.get(event_type)
        current = (
            EventState.ACTIVE
            if any(
                state == EventState.ACTIVE
                for key, state in self._topic_states.items()
                if key[0] == event_type
            )
            else EventState.INACTIVE
        )
        self._semantic_states[event_type] = current

        if notification.is_initial_value:
            return False
        return previous != current
