from __future__ import annotations

from datetime import datetime
from hashlib import sha256

from episode.plugins.models import PluginEvent

COMM_UPLOAD_VIDEO_INTERCOM_EVENT = 0x1132
COMM_ALARM_VIDEO_INTERCOM = 0x1133
DOORBELL_RINGING = 17
DISMISS_CALL = 18
UNLOCK_RECORD = 1

_ALARM_STRUCTURE_NAME = "NET_DVR_VIDEO_INTERCOM_ALARM"
_EVENT_STRUCTURE_NAME = "NET_DVR_VIDEO_INTERCOM_EVENT"
_DEVICE_NUMBER_OFFSET = 12
_DEVICE_NUMBER_LENGTH = 32
_ALARM_TYPE_OFFSET = 44
_EVENT_TYPE_OFFSET = 44
_PICTURE_TRANSPORT_OFFSET = 45
_UNLOCK_TYPE_OFFSET = 48
_CONTROL_SOURCE_OFFSET = 52
_CONTROL_SOURCE_LENGTH = 32
_PICTURE_LENGTH_OFFSET = 84
_LOCK_ID_OFFSET = 104
_LOCK_NAME_OFFSET = 108
_LOCK_NAME_LENGTH = 32
_EMPLOYEE_NUMBER_OFFSET = 140
_EMPLOYEE_NUMBER_LENGTH = 32
_MASK_STATUS_OFFSET = 172
_ALARM_LOCK_ID_OFFSET = 304
_ALARM_IOT_CHANNEL_OFFSET = 308
_MINIMUM_ALARM_SIZE = _ALARM_IOT_CHANNEL_OFFSET + 4
_MINIMUM_EVENT_SIZE = _MASK_STATUS_OFFSET + 1

_UNLOCK_METHODS = {
    1: "password",
    2: "hijacking",
    3: "card",
    4: "householder",
    5: "center_platform",
    6: "bluetooth",
    7: "qr_code",
    8: "face",
    9: "fingerprint",
    10: "dynamic_code",
}

_MASK_STATUSES = {
    0: "reserved",
    1: "unknown",
    2: "no_mask",
    3: "mask",
}


def _device_timestamp(payload: bytes) -> datetime | None:
    try:
        return datetime(
            year=int.from_bytes(payload[4:6], "little"),
            month=payload[6],
            day=payload[7],
            hour=payload[8],
            minute=payload[9],
            second=payload[10],
        )
    except (IndexError, ValueError):
        return None


def _text_field(payload: bytes, offset: int, length: int) -> str:
    raw = payload[offset : offset + length]
    return raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace").strip()


def _event_identity(device_id: str, observed_key: str, *parts: object) -> str:
    identity = "\x1f".join(
        ("hikvision-sdk", device_id, observed_key, *(str(part) for part in parts))
    )
    return sha256(identity.encode()).hexdigest()


def _interpret_doorbell_alarm(
    payload: bytes,
    device_id: str,
    received_at: datetime,
) -> PluginEvent | None:
    if len(payload) < _MINIMUM_ALARM_SIZE:
        return None
    declared_size = int.from_bytes(payload[:4], "little")
    if declared_size < _MINIMUM_ALARM_SIZE or declared_size > len(payload):
        return None

    alarm_type = payload[_ALARM_TYPE_OFFSET]
    if alarm_type == DOORBELL_RINGING:
        state = "active"
        phase = "ringing"
    elif alarm_type == DISMISS_CALL:
        state = "inactive"
        phase = "dismissed"
    else:
        return None

    device_timestamp = _device_timestamp(payload)
    observed_key = device_timestamp.isoformat() if device_timestamp else received_at.isoformat()
    metadata: dict[str, object] = {
        "vendor": "hikvision",
        "sdk_command": COMM_ALARM_VIDEO_INTERCOM,
        "sdk_structure": _ALARM_STRUCTURE_NAME,
        "structure_size": declared_size,
        "device_number": _text_field(payload, _DEVICE_NUMBER_OFFSET, _DEVICE_NUMBER_LENGTH),
        "alarm_type": alarm_type,
        "phase": phase,
        "lock_id": int.from_bytes(
            payload[_ALARM_LOCK_ID_OFFSET : _ALARM_LOCK_ID_OFFSET + 2], "little"
        ),
        "iot_channel_number": int.from_bytes(
            payload[_ALARM_IOT_CHANNEL_OFFSET : _ALARM_IOT_CHANNEL_OFFSET + 4],
            "little",
        ),
    }
    if device_timestamp:
        metadata["device_timestamp"] = device_timestamp.isoformat()

    return PluginEvent(
        timestamp=received_at,
        event_type="doorbell",
        event_state=state,
        source="hikvision:sdk",
        dedup_key=_event_identity(device_id, observed_key, "doorbell", state),
        metadata=metadata,
    )


def _interpret_unlock_record(
    payload: bytes,
    device_id: str,
    received_at: datetime,
) -> PluginEvent | None:
    if len(payload) < _MINIMUM_EVENT_SIZE:
        return None
    declared_size = int.from_bytes(payload[:4], "little")
    if declared_size < _MINIMUM_EVENT_SIZE or declared_size > len(payload):
        return None
    if payload[_EVENT_TYPE_OFFSET] != UNLOCK_RECORD:
        return None

    picture_length = int.from_bytes(
        payload[_PICTURE_LENGTH_OFFSET : _PICTURE_LENGTH_OFFSET + 4], "little"
    )
    if picture_length > len(payload) - declared_size:
        return None
    picture = payload[declared_size : declared_size + picture_length]
    unlock_type = payload[_UNLOCK_TYPE_OFFSET]
    lock_id = int.from_bytes(payload[_LOCK_ID_OFFSET : _LOCK_ID_OFFSET + 2], "little")
    device_timestamp = _device_timestamp(payload)
    observed_key = device_timestamp.isoformat() if device_timestamp else received_at.isoformat()

    metadata: dict[str, object] = {
        "vendor": "hikvision",
        "sdk_command": COMM_UPLOAD_VIDEO_INTERCOM_EVENT,
        "sdk_structure": _EVENT_STRUCTURE_NAME,
        "structure_size": declared_size,
        "sdk_event_type": UNLOCK_RECORD,
        "sdk_event_name": "unlock_record",
        "device_number": _text_field(payload, _DEVICE_NUMBER_OFFSET, _DEVICE_NUMBER_LENGTH),
        "unlock_type": unlock_type,
        "unlock_method": _UNLOCK_METHODS.get(unlock_type, "unknown"),
        "unlock_outcome": "not_reported_by_device",
        "control_source": _text_field(payload, _CONTROL_SOURCE_OFFSET, _CONTROL_SOURCE_LENGTH),
        "lock_id": lock_id,
        "lock_name": _text_field(payload, _LOCK_NAME_OFFSET, _LOCK_NAME_LENGTH),
        "employee_number": _text_field(payload, _EMPLOYEE_NUMBER_OFFSET, _EMPLOYEE_NUMBER_LENGTH),
        "mask_status": _MASK_STATUSES.get(payload[_MASK_STATUS_OFFSET], "unknown"),
        "picture_transport": ("binary" if payload[_PICTURE_TRANSPORT_OFFSET] == 0 else "url"),
        "picture_byte_size": picture_length,
    }
    if picture:
        picture_sha256 = sha256(picture).hexdigest()
        metadata["picture_sha256"] = picture_sha256
        metadata["embedded_picture"] = {
            "offset": declared_size,
            "byte_size": picture_length,
            "mime_type": "image/jpeg",
            "filename": "door-unlock.jpg",
            "sha256": picture_sha256,
        }
    if device_timestamp:
        metadata["device_timestamp"] = device_timestamp.isoformat()

    return PluginEvent(
        timestamp=received_at,
        event_type="door_access",
        event_state="active",
        source="hikvision:sdk",
        dedup_key=_event_identity(
            device_id,
            observed_key,
            "door_access",
            lock_id,
            unlock_type,
            metadata.get("picture_sha256", ""),
        ),
        metadata=metadata,
    )


def interpret_event(
    command: int,
    payload: bytes,
    device_id: str,
    received_at: datetime,
) -> PluginEvent | None:
    """Interpret only validated HCNetSDK event structures.

    Unknown commands and subtypes remain raw deliveries and are never guessed.
    """
    if command == COMM_ALARM_VIDEO_INTERCOM:
        return _interpret_doorbell_alarm(payload, device_id, received_at)
    if command == COMM_UPLOAD_VIDEO_INTERCOM_EVENT:
        return _interpret_unlock_record(payload, device_id, received_at)
    return None
