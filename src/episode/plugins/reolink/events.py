"""Reolink event parsing from binary Baichuan protocol frames.

Handles parsing of events received as binary frames over the TCP connection.
The camera pushes event frames (cmdId=33 for alarm events) that contain
XML payloads describing motion, person, vehicle, doorbell, and other events.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from episode.domain.event_filter import UNRECOGNIZED_EVENT_TYPE
from episode.plugins.reolink.client import (
    aes_decrypt_cfb,
    bc_decrypt,
    derive_aes_key,
)

logger = logging.getLogger(__name__)

EVENT_MAP = {
    # Audio
    "audio_detection": ("audio", "sound"),
    # Image
    "face_detection": ("face",),
    "human_detection": ("human", "person", "people"),
    "ladder_detection": ("ladder",),
    "line_crossing_detection": ("line", "cross"),
    "loitering_detection": ("loitering",),
    "motion_detection": ("motion", "movement", "mov", "md"),
    "package_detection": ("package",),
    "pet_detection": ("pet", "animal", "dog_cat", "dog", "cat"),
    "vehicle_detection": ("vehicle", "car"),
    # Other events
    # ``tampering_detection`` is the spelling Episode canonicalizes tamper to;
    # ``tamper_detection`` is kept because that is what the ONVIF adapter emits
    # for the same physical state, and one camera's tamper must not sit in a
    # different class from another's. ``vt`` is the vendor's own marker.
    "tampering_detection": ("tamper", "vt"),
    "doorbell": ("doorbell", "ring", "visitor"),
}


def _iter_texts(value: Any) -> list[str]:
    """Recursively collect string values from dict/list/tuple structures.

    Reolink alarm payloads often carry the subtype as a list, e.g.
    ``type=["intrusion", "people"]`` or a nested ``_value`` array, so we walk
    the whole structure to find the discriminating keyword.
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for v in value.values():
            out.extend(_iter_texts(v))
        return out
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            out.extend(_iter_texts(item))
        return out
    return []


ACTIVE_STATES = {"true", "1", "active", "on", "start", "begin"}
INACTIVE_STATES = {"false", "0", "inactive", "off", "stop", "end"}


@dataclass(frozen=True)
class ReolinkEvent:
    """Normalized Reolink event derived from Baichuan protocol frame payload."""

    event_type: str
    event_state: str
    source: str = "reolink:events"
    timestamp: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)  # type: ignore[assignment]
    raw_payload: dict[str, Any] | None = None
    device_id: str = ""
    channel: int = 0
    event_id: str = ""

    def __post_init__(self):
        """Default the timestamp to now if not provided."""
        if self.timestamp is None:
            object.__setattr__(self, "timestamp", datetime.now(tz=timezone.utc))


def parse_alarm_event_frame(
    body: bytes,
    channel: int = 0,
    *,
    nonce: str = "",
    password: str = "",
    use_aes: bool = False,
) -> list[ReolinkEvent]:
    """Parse a Baichuan alarm event frame body (cmdId=33).

    The camera pushes binary frames containing XML event data.
    This method handles both encrypted and plain XML bodies.

    Args:
        body: Raw bytes from the TCP frame (may be encrypted)
        channel: Channel number used as the BC cipher offset (from the
            connection's negotiated host channel id).
        nonce: Nonce negotiated during login (required for AES decryption).
        password: Camera password (required for AES key derivation).
        use_aes: Whether the body is AES-128-CFB encrypted (vs. XOR BC).

    Returns:
        List of parsed ReolinkEvent objects
    """
    events: list[ReolinkEvent] = []

    # Try to decrypt first using the negotiated encryption mode
    decrypted_body = _maybe_decrypt(
        body,
        offset=channel,
        nonce=nonce,
        password=password,
        use_aes=use_aes,
    )

    # Parse XML
    root = _parse_xml(decrypted_body)
    if root is None:
        logger.warning("Failed to parse alarm event XML body: %s", decrypted_body[:200])
        return []

    # Extract events from various possible structures
    events.extend(_extract_events_from_root(root, channel))

    logger.debug("Parsed %d events from alarm frame", len(events))
    return events


def parse_battery_status_frame(
    body: bytes,
    channel: int = 0,
    *,
    nonce: str = "",
    password: str = "",
    use_aes: bool = False,
) -> ReolinkEvent | None:
    """Parse a battery status frame (cmdId=252).

    Args:
        body: Raw bytes from the TCP frame
        channel: Channel number used as the BC cipher offset.
        nonce: Nonce negotiated during login (required for AES decryption).
        password: Camera password (required for AES key derivation).
        use_aes: Whether the body is AES-128-CFB encrypted (vs. XOR BC).

    Returns:
        ReolinkEvent with battery info or None
    """
    decrypted_body = _maybe_decrypt(
        body,
        offset=channel,
        nonce=nonce,
        password=password,
        use_aes=use_aes,
    )
    root = _parse_xml(decrypted_body)
    if root is None:
        return None

    metadata: dict[str, Any] = {}
    battery_level = 0
    charging = False

    for elem in root:
        if elem.tag == "batteryPower":
            try:
                battery_level = int(elem.text or "0")
            except (ValueError, TypeError):
                pass
        elif elem.tag == "isCharging":
            charging = str(elem.text or "false").lower() in ("true", "1", "yes")
        elif elem.tag == "lowPowerAlarm":
            metadata["low_power"] = str(elem.text or "false")

    event_type = "battery_low" if battery_level < 20 else "battery_status"
    state = "charging" if charging else "discharging"

    return ReolinkEvent(
        event_type=event_type,
        event_state=state,
        timestamp=datetime.now(tz=timezone.utc),
        metadata={**metadata, "battery_level": battery_level, "charging": charging},
    )


def interpret_event(raw_payload: dict[str, Any]) -> ReolinkEvent:
    """Map a parsed Reolink event payload dict to a normalized event.

    This handles events that have already been parsed from XML into dicts.

    Args:
        raw_payload: Dict from _xml_to_dict() for a single event

    Returns:
        Normalized ReolinkEvent
    """
    payload = raw_payload
    if len(payload) == 1 and next(iter(payload)) in {"AlarmEvent", "Event"}:
        nested = next(iter(payload.values()))
        if isinstance(nested, dict):
            payload = nested

    # Extract timestamp
    timestamp = _extract_timestamp(payload)

    # Map event type
    event_type = _map_event_type(payload)
    event_state = _map_event_state(payload)

    metadata = {
        "integration": "reolink",
        "raw_payload": raw_payload,
    }

    # Extract common fields
    event_id = payload.get("eventID", "") or payload.get("EventIndex", "")
    channel = payload.get("channel", payload.get("channelId", 0))
    if isinstance(channel, str):
        try:
            channel = int(channel)
        except (ValueError, TypeError):
            channel = 0

    if event_id:
        metadata["event_id"] = event_id
    if channel:
        metadata["channel"] = channel

    logger.debug(
        "Interpreted event: type=%s state=%s",
        event_type,
        event_state,
    )

    return ReolinkEvent(
        event_type=event_type,
        event_state=event_state,
        timestamp=timestamp,
        metadata=metadata,
        raw_payload=raw_payload,
        channel=channel,
        event_id=str(event_id),
    )


# ── Internal helpers ──────────────────────────────────────────────────


def _maybe_decrypt(
    data: bytes,
    offset: int = 0,
    *,
    nonce: str = "",
    password: str = "",
    use_aes: bool = False,
) -> bytes:
    """Try to decrypt data using the negotiated encryption mode.

    Tries AES-128-CFB first (if use_aes and key material present), then
    XOR BCEncrypt with the channel offset. Returns the decrypted bytes if
    they parse as XML, otherwise the original data.
    """
    candidates: list[bytes] = []
    if use_aes and nonce:
        try:
            key = derive_aes_key(nonce, password)
            candidates.append(aes_decrypt_cfb(data, key))
        except Exception:
            pass
    candidates.append(bc_decrypt(data, offset))

    for decrypted in candidates:
        try:
            # Check if the decrypted result looks like XML (starts with '<').
            stripped = decrypted.lstrip()[:16]
            if stripped.startswith(b"<?xml") or stripped.startswith(b"<"):
                # Make sure it's valid XML
                ET.fromstring(decrypted)
                return decrypted
        except Exception:
            continue

    return data


def _parse_xml(data: bytes):
    """Parse XML data, returning Element or None on failure."""
    try:
        return ET.fromstring(data)
    except Exception as e:
        logger.debug("XML parse failed: %s", e)
        return None


def _extract_events_from_root(root, channel: int) -> list[ReolinkEvent]:
    """Extract event objects from an XML root element converted to a dictionary."""
    events: list[ReolinkEvent] = []

    # Convert the entire XML tree to a dictionary
    full_dict = _xml_to_dict(root)
    if not full_dict:
        return events

    # Get the root tag and its content
    root_tag = list(full_dict.keys())[0]
    content = full_dict[root_tag]

    event_dicts = []

    # Helper function to extract elements and keep their wrapping tag
    def _extract_wrapped(parent_dict: dict, tag: str) -> list[dict]:
        if not isinstance(parent_dict, dict) or tag not in parent_dict:
            return []
        items = parent_dict[tag]
        if not isinstance(items, list):
            items = [items]
        # Returns in the {"Tag": {...}} format that interpret_event expects
        return [{tag: item} for item in items]

    if isinstance(content, dict):
        # 1 and 2. Root -> AlarmEventList -> AlarmEvent / Event
        if "AlarmEventList" in content:
            alarm_lists = content["AlarmEventList"]
            if not isinstance(alarm_lists, list):
                alarm_lists = [alarm_lists]

            for al in alarm_lists:
                if isinstance(al, dict):
                    event_dicts.extend(_extract_wrapped(al, "Event"))
                    event_dicts.extend(_extract_wrapped(al, "AlarmEvent"))
        else:
            # 3 and 5. Direct Event / AlarmEvent elements
            event_dicts.extend(_extract_wrapped(content, "Event"))
            event_dicts.extend(_extract_wrapped(content, "AlarmEvent"))

            # 4. Root -> param -> AlarmEventList -> AlarmEvent/Event
            if "param" in content:
                params = content["param"]
                if not isinstance(params, list):
                    params = [params]

                for p in params:
                    if isinstance(p, dict):
                        if "AlarmEventList" in p:
                            al_list = p["AlarmEventList"]
                            if not isinstance(al_list, list):
                                al_list = [al_list]
                            for al in al_list:
                                if isinstance(al, dict):
                                    event_dicts.extend(_extract_wrapped(al, "Event"))
                                    event_dicts.extend(_extract_wrapped(al, "AlarmEvent"))
                        else:
                            event_dicts.extend(_extract_wrapped(p, "Event"))
                            event_dicts.extend(_extract_wrapped(p, "AlarmEvent"))

    # If no specific structure is found, treat the entire dictionary as a single event
    if not event_dicts and full_dict:
        event_dicts = [full_dict]

    for event_dict in event_dicts:
        event = interpret_event(event_dict)
        if event.channel == 0 and channel:
            event = _replace_field(event, "channel", channel)
        events.append(event)

    return events


def _find_event_children(parent) -> list:
    """Recursively find Event / AlarmEvent elements in XML subtree."""
    events = []
    for child in parent:
        if child.tag in ("Event", "AlarmEvent"):
            events.append(child)
        else:
            events.extend(_find_event_children(child))
    return events


def _xml_to_dict(element) -> dict[str, Any]:
    """Convert an XML element to a dict"""
    result: dict[str, Any] = {}

    if element.attrib:
        result["@attributes"] = dict(element.attrib)

    children = list(element)
    text = (element.text or "").strip()

    if not children:
        # Leaf Node
        val: Any = text
        if text:
            try:
                val = int(text)
            except ValueError:
                try:
                    val = float(text)
                except ValueError:
                    pass

        # return {tag: valor}
        if not element.attrib:
            return {element.tag: val if val != "" else ""}

        # handle attributes
        if val != "":
            result["_value"] = val
        return {element.tag: result}

    # Process children
    for child in children:
        child_dict = _xml_to_dict(child)
        for key, val in child_dict.items():
            if key in result:
                if not isinstance(result[key], list):
                    result[key] = [result[key]]
                result[key].append(val)
            else:
                result[key] = val

    return {element.tag: result}


def _extract_timestamp(payload: dict[str, Any]) -> datetime:
    """Extract timestamp from event payload."""
    # Check various possible locations
    for key in (
        "time",
        "timestamp",
        "timeStamp",
        "createTime",
        "eventTime",
        "EventTime",
        "AlarmTime",
    ):
        value = payload.get(key)
        if value:
            parsed = _try_parse_timestamp(value)
            if parsed:
                return parsed

    # Check nested
    for key in ("param", "AlarmEventList"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            for subkey in ("time", "timestamp", "createTime", "eventTime"):
                value = nested.get(subkey)
                if value:
                    parsed = _try_parse_timestamp(value)
                    if parsed:
                        return parsed

    return datetime.now(tz=timezone.utc)


def _try_parse_timestamp(value) -> datetime | None:
    """Parse a timestamp string to datetime."""
    if not value:
        return None
    try:
        if isinstance(value, str):
            # Handle various formats
            cleaned = value.strip()
            if cleaned.endswith("Z"):
                cleaned = cleaned[:-1] + "+00:00"
            parsed = datetime.fromisoformat(cleaned)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
    except (ValueError, TypeError):
        pass

    # Try as integer (Unix timestamp)
    try:
        ts = float(value)
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (ValueError, TypeError, OSError):
        pass

    return None


def _map_event_type(payload: dict[str, Any]) -> str:
    """Map event payload to canonical event type string."""
    # Check various possible keys at the root level of the event payload.
    cmd = payload.get("cmd", "")
    event_type_val = payload.get("type", "")
    channel_event = payload.get("channelEvent", "")
    event_type = payload.get("eventType", "")
    ai_type = payload.get("AItype") or payload.get("aiType") or payload.get("aitype") or ""
    status = payload.get("status", "")

    def get_strings_to_check():
        """Yield candidate type strings in priority order: AI types first,
        then fallback to generic status and nested payload texts."""

        # 1: Direct AI type (ignore "none" so it doesn't mask valid nested types)
        if str(ai_type).lower() != "none":
            yield str(ai_type).lower()

        # 2: Baichuan smartAiTypeList structure
        smart_ai_list = payload.get("smartAiTypeList", {})
        if isinstance(smart_ai_list, dict):
            smart_ai = smart_ai_list.get("smartAiType", {})
            # Ensure it is iterable
            if not isinstance(smart_ai, list):
                smart_ai = [smart_ai]

            for ai_item in smart_ai:
                if isinstance(ai_item, dict):
                    # Yield main type (e.g., "intrusion")
                    yield str(ai_item.get("type", "")).lower()

                    # Yield sub-types (e.g., "people", "dog_cat")
                    sub_list = ai_item.get("subList", {})
                    if isinstance(sub_list, dict):
                        sub_types = sub_list.get("type", [])
                        if not isinstance(sub_types, list):
                            sub_types = [sub_types]
                        for st in sub_types:
                            yield str(st).lower()

        # 3: Common event string fields
        yield str(cmd or event_type_val or channel_event or event_type or "").lower()

        # 4: Status field (catches generic "MD" motion detection)
        yield str(status).lower()

        # 5: Dict 'param'
        param = payload.get("param")
        if isinstance(param, dict):
            for key in ("type", "event", "event_type"):
                val = param.get(key, "")
                if isinstance(val, str) and val.strip():
                    yield val.lower()

        # 6: All string values anywhere in the payload as a last resort
        try:
            for text in _iter_texts(payload):
                if text and text.strip() and str(text).lower() != "none":
                    yield text.lower()
        except NameError:
            pass  # Fallback in case _iter_texts is not available in the namespace

    for text in get_strings_to_check():
        if not text:
            continue
        for event_result, keywords in EVENT_MAP.items():
            if any(keyword in text for keyword in keywords):
                return event_result

    if cmd:
        return str(cmd).lower().replace("-", "_").replace(" ", "_")

    # Nothing in this frame is recognized and it carries no command name, so the
    # honest answer is "unrecognized": the core classifies it as ``unknown``, it
    # is never filtered, and it stays queryable. Reporting a filterable class
    # here (a ``system`` heartbeat, previously) would let one camera's real but
    # unnamed signal be suppressed by a profile while an identical signal from
    # another camera could not be.
    return UNRECOGNIZED_EVENT_TYPE


def _map_event_state(payload: dict[str, Any]) -> str:
    """Map event state to 'active' or 'inactive'.

    Reolink alarm push frames (cmdId=33) are detections: the camera pushes a
    frame at the moment of detection (motion, person, vehicle, ...). Many of
    these payloads carry no explicit state field (e.g. ``status="none"``), so
    default to ``"active"``. This is what lets the Episode engine create and
    link an Episode: it only correlates ACTIVE events, or INACTIVE events that
    follow a matching preceding ACTIVE transition. Without this default, every
    detection would be stored as ``inactive`` with no preceding active event,
    so no Episode would ever be created or linked.
    """

    def get_state_values():
        """Yield candidate state fields from the payload and nested param."""
        for key in ("state", "status", "active", "enabled"):
            yield payload.get(key)

        param = payload.get("param")
        if isinstance(param, dict):
            for key in ("state", "status", "active"):
                yield param.get(key)

    for value in get_state_values():
        if value is not None:
            val_str = str(value).lower()
            if val_str in ACTIVE_STATES:
                return "active"
            if val_str in INACTIVE_STATES:
                return "inactive"

    # A pushed detection without an explicit state is activity.
    return "active"


def _replace_field(obj, field_name: str, new_value: Any):
    """Create a copy of a frozen dataclass with a field replaced."""
    return replace(obj, **{field_name: new_value})
