from datetime import datetime, timedelta, timezone
from hashlib import sha256

from episode.plugins.hikvision.sdk.events import (
    COMM_ALARM_VIDEO_INTERCOM,
    COMM_UPLOAD_VIDEO_INTERCOM_EVENT,
    DISMISS_CALL,
    DOORBELL_RINGING,
    interpret_event,
)


def _video_intercom_payload(alarm_type: int) -> bytes:
    payload = bytearray(560)
    payload[:4] = len(payload).to_bytes(4, "little")
    payload[4:6] = (2026).to_bytes(2, "little")
    payload[6:11] = bytes((8, 3, 12, 33, 17))
    payload[12:23] = b"10010100000"
    payload[44] = alarm_type
    return bytes(payload)


def _unlock_record_payload(picture: bytes = b"jpeg bytes") -> bytes:
    structure_size = 568
    payload = bytearray(structure_size + len(picture))
    payload[:4] = structure_size.to_bytes(4, "little")
    payload[4:6] = (2026).to_bytes(2, "little")
    payload[6:11] = bytes((8, 5, 15, 47, 5))
    payload[12:23] = b"10010100000"
    payload[44] = 1
    payload[45] = 0
    payload[48] = 4
    payload[52:63] = b"10010110001"
    payload[84:88] = len(picture).to_bytes(4, "little")
    payload[104:106] = (1).to_bytes(2, "little")
    payload[108:113] = b"Door2"
    payload[568:] = picture
    return bytes(payload)


def test_interprets_validated_doorbell_ring_callback():
    received_at = datetime(2026, 8, 3, 11, 33, 16, tzinfo=timezone.utc)

    event = interpret_event(
        COMM_ALARM_VIDEO_INTERCOM,
        _video_intercom_payload(DOORBELL_RINGING),
        "front-doorbell",
        received_at,
    )

    assert event is not None
    assert event.timestamp == received_at
    assert event.event_type == "doorbell"
    assert event.event_state == "active"
    assert event.source == "hikvision:sdk"
    assert event.metadata == {
        "vendor": "hikvision",
        "sdk_command": COMM_ALARM_VIDEO_INTERCOM,
        "sdk_structure": "NET_DVR_VIDEO_INTERCOM_ALARM",
        "structure_size": 560,
        "device_number": "10010100000",
        "alarm_type": DOORBELL_RINGING,
        "phase": "ringing",
        "lock_id": 0,
        "iot_channel_number": 0,
        "device_timestamp": "2026-08-03T12:33:17",
    }


def test_interprets_dismiss_as_inactive_doorbell_event():
    event = interpret_event(
        COMM_ALARM_VIDEO_INTERCOM,
        _video_intercom_payload(DISMISS_CALL),
        "front-doorbell",
        datetime(2026, 8, 3, 11, 34, 17, tzinfo=timezone.utc),
    )

    assert event is not None
    assert event.event_type == "doorbell"
    assert event.event_state == "inactive"
    assert event.metadata["phase"] == "dismissed"


def test_interprets_unlock_record_and_embedded_picture_without_claiming_success():
    picture = b"\xff\xd8door snapshot\xff\xd9"
    received_at = datetime(2026, 8, 5, 14, 47, 6, tzinfo=timezone.utc)

    event = interpret_event(
        COMM_UPLOAD_VIDEO_INTERCOM_EVENT,
        _unlock_record_payload(picture),
        "front-doorbell",
        received_at,
    )

    assert event is not None
    assert event.timestamp == received_at
    assert event.event_type == "door_access"
    assert event.event_state == "active"
    assert event.metadata == {
        "vendor": "hikvision",
        "sdk_command": COMM_UPLOAD_VIDEO_INTERCOM_EVENT,
        "sdk_structure": "NET_DVR_VIDEO_INTERCOM_EVENT",
        "structure_size": 568,
        "sdk_event_type": 1,
        "sdk_event_name": "unlock_record",
        "device_number": "10010100000",
        "unlock_type": 4,
        "unlock_method": "householder",
        "unlock_outcome": "not_reported_by_device",
        "control_source": "10010110001",
        "lock_id": 1,
        "lock_name": "Door2",
        "employee_number": "",
        "mask_status": "reserved",
        "picture_transport": "binary",
        "picture_byte_size": len(picture),
        "picture_sha256": sha256(picture).hexdigest(),
        "device_timestamp": "2026-08-05T15:47:05",
    }


def test_device_timestamp_makes_repeated_callback_identity_stable():
    payload = _video_intercom_payload(DOORBELL_RINGING)
    first = interpret_event(
        COMM_ALARM_VIDEO_INTERCOM,
        payload,
        "front-doorbell",
        datetime(2026, 8, 3, 11, 33, 16, tzinfo=timezone.utc),
    )
    repeated = interpret_event(
        COMM_ALARM_VIDEO_INTERCOM,
        payload,
        "front-doorbell",
        datetime(2026, 8, 3, 11, 33, 16, tzinfo=timezone.utc) + timedelta(milliseconds=50),
    )

    assert first is not None
    assert repeated is not None
    assert first.dedup_key == repeated.dedup_key


def test_unknown_or_invalid_callbacks_remain_uninterpreted():
    received_at = datetime.now(tz=timezone.utc)
    assert interpret_event(0x4000, b"anything", "front-doorbell", received_at) is None
    assert (
        interpret_event(
            COMM_ALARM_VIDEO_INTERCOM,
            _video_intercom_payload(99),
            "front-doorbell",
            received_at,
        )
        is None
    )
    assert (
        interpret_event(
            COMM_ALARM_VIDEO_INTERCOM,
            b"short",
            "front-doorbell",
            received_at,
        )
        is None
    )

    invalid_picture = bytearray(_unlock_record_payload())
    invalid_picture[84:88] = (9999).to_bytes(4, "little")
    assert (
        interpret_event(
            COMM_UPLOAD_VIDEO_INTERCOM_EVENT,
            bytes(invalid_picture),
            "front-doorbell",
            received_at,
        )
        is None
    )
