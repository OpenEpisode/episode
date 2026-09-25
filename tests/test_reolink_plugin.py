"""Unit tests for the Reolink plugin.

Covers crypto round-trips, event frame parsing, device config validation,
plugin lifecycle, status aggregation, registry selection, and device
validation result mapping. All tests use mocks; no live camera is required.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime, timezone

import pytest

from episode.domain.models import CapabilityConfig, Device
from episode.media.registry import CameraMedia, MediaRegistry
from episode.plugins.models import (
    PluginContext,
    PluginInstanceState,
    PluginInstanceStatus,
    PluginState,
)
from episode.plugins.registry import builtin_plugin_registry
from episode.plugins.reolink.client import (
    BaichuanFrameDispatcher,
    ReolinkError,
    aes_encrypt_cfb,
    bc_decrypt,
    bc_encrypt,
    derive_aes_key,
)
from episode.plugins.reolink.device import (
    DEFAULT_DEDUP_WINDOW,
    DEFAULT_SNAPSHOT_TIMEOUT,
    ReolinkDeviceConfig,
    ReolinkDeviceConnection,
    device_config,
)
from episode.plugins.reolink.events import (
    interpret_event,
    parse_alarm_event_frame,
    parse_battery_status_frame,
)
from episode.plugins.reolink.plugin import ReolinkPlugin
from episode.plugins.reolink.preview import (
    DEFAULT_PREVIEW_TIMEOUT,
    PreviewSettings,
    PreviewStats,
)
from episode.plugins.reolink.snapshot_slot import SnapshotPrearmSettings, SnapshotSlot

# ---------------------------------------------------------------------------
# Crypto round-trips
# ---------------------------------------------------------------------------


def test_bc_encrypt_decrypt_round_trip():
    payload = b"<Event><cmd>MotionDetect</cmd></Event>"
    encrypted = bc_encrypt(payload, offset=0)
    assert encrypted != payload
    assert bc_decrypt(encrypted, offset=0) == payload


def test_bc_cipher_respects_offset():
    payload = b"MotionDetect"
    assert bc_encrypt(payload, offset=0) != bc_encrypt(payload, offset=1)
    assert bc_decrypt(bc_encrypt(payload, offset=3), offset=3) == payload


def test_derive_aes_key_is_stable_16_bytes():
    key = derive_aes_key("nonce123", "hunter2")
    assert isinstance(key, bytes)
    assert len(key) == 16
    assert derive_aes_key("nonce123", "hunter2") == key
    assert derive_aes_key("nonce123", "other") != key


# ---------------------------------------------------------------------------
# Event frame parsing
# ---------------------------------------------------------------------------


def test_parse_alarm_event_frame_motion():
    xml = b"<Event><cmd>MotionDetect</cmd><state>true</state><channel>0</channel></Event>"
    events = parse_alarm_event_frame(xml, channel=0)
    assert len(events) == 1
    assert events[0].event_type == "motion_detection"
    assert events[0].event_state == "active"


def test_parse_alarm_event_frame_person():
    xml = b"<Event><cmd>PersonDetect</cmd><state>true</state></Event>"
    events = parse_alarm_event_frame(xml, channel=0)
    assert events[0].event_type == "human_detection"


def test_parse_alarm_event_frame_encrypted_body():
    xml = b"<Event><cmd>MotionDetect</cmd><state>1</state></Event>"
    encrypted = bc_encrypt(xml, offset=0)
    events = parse_alarm_event_frame(encrypted, channel=0)
    assert len(events) == 1
    assert events[0].event_type == "motion_detection"


def test_parse_alarm_event_frame_alarm_event_human():
    """Reolink native AlarmEventList -> AlarmEvent with AItype must map to
    human_detection, not the generic 'system' type."""
    xml = (
        b"<AlarmEventList>"
        b"<AlarmEvent>"
        b"<channelId>0</channelId>"
        b"<status>true</status>"
        b"<AItype>Human</AItype>"
        b"<timeStamp>1756000000</timeStamp>"
        b"</AlarmEvent>"
        b"</AlarmEventList>"
    )
    events = parse_alarm_event_frame(xml, channel=0)
    assert len(events) == 1
    assert events[0].event_type == "human_detection"
    assert events[0].event_state == "active"


def test_parse_alarm_event_frame_alarm_event_vehicle():
    xml = (
        b"<AlarmEventList>"
        b"<AlarmEvent>"
        b"<channelId>0</channelId>"
        b"<status>true</status>"
        b"<AItype>Vehicle</AItype>"
        b"</AlarmEvent>"
        b"</AlarmEventList>"
    )
    events = parse_alarm_event_frame(xml, channel=0)
    assert len(events) == 1
    assert events[0].event_type == "vehicle_detection"


def test_parse_alarm_event_frame_type_list_people():
    """Reolink push payloads carry the subtype in a list-valued 'type' field
    (e.g. type=['intrusion','people']). These must map to human_detection,
    not 'system'."""
    payload = {
        "@attributes": [{"version": "1.1"}, {"version": "1.1"}],
        "_value": [0, "none", "none", 0, 0, ["intrusion", 1, [0, "people"], 301640950544, 3046504]],
        "channelId": 0,
        "status": "none",
        "AItype": "none",
        "recording": 0,
        "timeStamp": 0,
        "type": ["intrusion", "people"],
        "index": [1, 0],
        "pts": 301640950544,
        "frameIndex": 3046504,
    }
    event = interpret_event(payload)
    assert event.event_type == "human_detection"


def test_parse_alarm_event_frame_type_list_vehicle():
    payload = {
        "type": ["intrusion", "vehicle"],
        "AItype": "none",
        "status": "none",
    }
    event = interpret_event(payload)
    assert event.event_type == "vehicle_detection"


def test_alarm_detection_defaults_to_active_state():
    """Reolink alarm pushes are detections: without an explicit state field
    they must map to 'active' so the Episode engine can create/link an
    Episode (it ignores 'inactive' events with no preceding 'active' one)."""
    payload = {
        "type": ["intrusion", "people"],
        "AItype": "none",
        "status": "none",
    }
    event = interpret_event(payload)
    assert event.event_state == "active"


def test_alarm_explicit_inactive_state_is_honored():
    """An explicit inactive/false state must still map to 'inactive'."""
    payload = {"type": "motion", "status": "false"}
    event = interpret_event(payload)
    assert event.event_state == "inactive"


def test_parse_alarm_event_frame_multiple_alarm_events():
    xml = (
        b"<AlarmEventList>"
        b"<AlarmEvent><channelId>0</channelId><AItype>Human</AItype></AlarmEvent>"
        b"<AlarmEvent><channelId>0</channelId><AItype>Vehicle</AItype></AlarmEvent>"
        b"</AlarmEventList>"
    )
    events = parse_alarm_event_frame(xml, channel=0)
    assert len(events) == 2
    assert events[0].event_type == "human_detection"
    assert events[1].event_type == "vehicle_detection"


def test_parse_wrapped_event_preserves_state_identity_and_timestamp():
    xml = (
        b"<Event><cmd>AudioDetect</cmd><state>false</state><channelId>2</channelId>"
        b"<eventID>event-42</eventID><timeStamp>1756000000</timeStamp></Event>"
    )

    events = parse_alarm_event_frame(xml, channel=0)

    assert len(events) == 1
    assert events[0].event_type == "audio_detection"
    assert events[0].event_state == "inactive"
    assert events[0].channel == 2
    assert events[0].event_id == "event-42"
    assert events[0].timestamp == datetime.fromtimestamp(1756000000, tz=timezone.utc)


def test_parse_smart_ai_event_uses_detected_subtype():
    xml = (
        b"<AlarmEventList><AlarmEvent><status>none</status><smartAiTypeList>"
        b"<smartAiType><type>intrusion</type><subList><type>people</type></subList>"
        b"</smartAiType></smartAiTypeList></AlarmEvent></AlarmEventList>"
    )

    events = parse_alarm_event_frame(xml, channel=0)

    assert len(events) == 1
    assert events[0].event_type == "human_detection"
    assert events[0].event_state == "active"


def test_parse_alarm_event_frame_invalid_returns_empty():
    assert parse_alarm_event_frame(b"\x00\x01\x02", channel=0) == []


def test_parse_alarm_event_frame_aes_encrypted_body():
    xml = b"<Event><cmd>MotionDetect</cmd><state>true</state></Event>"
    nonce = "nonce123"
    password = "hunter2"
    key = derive_aes_key(nonce, password)
    encrypted = aes_encrypt_cfb(xml, key)
    events = parse_alarm_event_frame(
        encrypted,
        channel=0,
        nonce=nonce,
        password=password,
        use_aes=True,
    )
    assert len(events) == 1
    assert events[0].event_type == "motion_detection"


def test_parse_alarm_event_frame_aes_without_params_returns_empty():
    """An AES body without decryption params must not be misparsed as XML."""
    xml = b"<Event><cmd>MotionDetect</cmd><state>true</state></Event>"
    key = derive_aes_key("nonce123", "hunter2")
    encrypted = aes_encrypt_cfb(xml, key)
    events = parse_alarm_event_frame(encrypted, channel=0)
    assert events == []


def test_parse_battery_status_frame():
    xml = (
        b"<batteryStatus><batteryPower>85</batteryPower>"
        b"<isCharging>false</isCharging></batteryStatus>"
    )
    event = parse_battery_status_frame(xml, channel=0)
    assert event is not None
    assert event.event_type == "battery_status"
    assert event.metadata["battery_level"] == 85
    assert event.metadata["charging"] is False


def test_parse_battery_low_frame():
    xml = (
        b"<batteryStatus><batteryPower>12</batteryPower>"
        b"<isCharging>true</isCharging></batteryStatus>"
    )
    event = parse_battery_status_frame(xml, channel=0)
    assert event is not None
    assert event.event_type == "battery_low"
    assert event.event_state == "charging"


# ---------------------------------------------------------------------------
# Frame dispatcher (single-consumer event routing)
# ---------------------------------------------------------------------------


class FakeFrameReader:
    """Minimal stand-in for BaichuanFrameReader that yields pre-built frames."""

    def __init__(self, frames):
        self._frames = frames

    async def iter_frames(self):
        for cmd_id, msg_num, resp_code, payload_offset, body in self._frames:
            yield cmd_id, msg_num, resp_code, payload_offset, body


@pytest.mark.asyncio
async def test_dispatcher_routes_response_and_events_without_dropping():
    # Interleave a command response (cmd 146) with an alarm push (cmd 33).
    # The alarm push arrives *inside* the response window, which previously
    # would have been discarded by the command's response reader.
    reader = FakeFrameReader(
        [
            (146, 1, 0, 0, b"<StreamInfoList/>"),  # response to get_stream_url
            (33, 0, 0, 0, b"<AlarmEventList/>"),  # alarm push during response window
        ]
    )
    dispatcher = BaichuanFrameDispatcher(reader)
    # Register waiters BEFORE starting the dispatcher, matching the real flow
    # where a waiter is registered before the command is sent.
    request_task = asyncio.create_task(dispatcher.request(146, timeout=5.0))
    event_task = asyncio.create_task(dispatcher.events().__anext__())
    dispatcher.start()

    try:
        resp = await asyncio.wait_for(request_task, timeout=5.0)
        assert resp[0] == 146
        assert b"StreamInfoList" in resp[3]

        event_cmd, event_body = await asyncio.wait_for(event_task, timeout=5.0)
        assert event_cmd == 33
        assert b"AlarmEventList" in event_body
    finally:
        await dispatcher.stop()


@pytest.mark.asyncio
async def test_dispatcher_eof_fails_and_removes_waiter():
    reader = FakeFrameReader([])  # no frames arrive
    dispatcher = BaichuanFrameDispatcher(reader)
    dispatcher.start()
    try:
        with pytest.raises(ConnectionError):
            await dispatcher.request(146, timeout=0.05)
        # The waiter must be cleaned up so a later frame isn't misrouted
        assert all(fut.done() for _, fut in dispatcher._waiters)
    finally:
        await dispatcher.stop()


# ---------------------------------------------------------------------------
# device_config validation
# ---------------------------------------------------------------------------


def _valid_device_mapping(**overrides):
    mapping = {
        "id": "cam-1",
        "name": "Front Camera",
        "area_id": "area-1",
        "ip_address": "192.168.1.10",
        "username": "admin",
        "password": "secret",
        "configs": {"reolink": {"port": 9000, "settings": {"host": "10.0.0.5"}}},
    }
    mapping.update(overrides)
    return mapping


def test_device_config_valid():
    config, error = device_config(_valid_device_mapping())
    assert error is None
    assert config is not None
    assert config.host == "10.0.0.5"
    assert config.api_port == 9000


def test_device_config_missing_credentials():
    config, error = device_config(_valid_device_mapping(username="", password=""))
    assert config is None
    assert "credentials" in error.lower()


def test_device_config_missing_network_address():
    config, error = device_config(_valid_device_mapping(ip_address=""))
    assert config is None
    assert error is not None


def test_device_config_invalid_port():
    config, error = device_config(_valid_device_mapping(configs={"reolink": {"port": 70000}}))
    assert config is None
    assert "port" in error.lower()


def test_device_config_negative_timeout():
    config, error = device_config(
        _valid_device_mapping(configs={"reolink": {"port": 9000, "settings": {"timeout": -1}}})
    )
    assert config is None
    assert "timeout" in error.lower()


def test_device_config_media_enabled_parses_setting():
    config, error = device_config(
        _valid_device_mapping(
            configs={"reolink": {"port": 9000, "settings": {"media_enabled": True}}}
        )
    )
    assert error is None
    assert config is not None
    assert config.media_enabled is True


def test_device_config_media_disabled_by_default():
    config, error = device_config(_valid_device_mapping())
    assert error is None
    assert config is not None
    assert config.media_enabled is False


def test_device_config_events_enabled_parses_setting():
    config, error = device_config(
        _valid_device_mapping(
            configs={"reolink": {"port": 9000, "settings": {"events_enabled": True}}}
        )
    )
    assert error is None
    assert config is not None
    assert config.events_enabled is True


def test_device_config_events_disabled_by_default():
    config, error = device_config(_valid_device_mapping())
    assert error is None
    assert config is not None
    assert config.events_enabled is False


# ---------------------------------------------------------------------------
# Media registration (streams + snapshots)
# ---------------------------------------------------------------------------


class FakeMediaRegistry:
    def __init__(self):
        self.sources: dict[str, CameraMedia] = {}

    def register(self, source: CameraMedia) -> None:
        self.sources[source.device_id] = source

    def get(self, device_id: str) -> CameraMedia | None:
        return self.sources.get(device_id)

    def unregister(self, device_id: str, *, source: str | None = None) -> None:
        self.sources.pop(device_id, None)


class FakeBaichuanClient:
    def __init__(self, *, stream_ok=True, snapshot=b"\xff\xd8\xff\xe0" + b"\x00" * 8 + b"\xff\xd9"):
        self._stream_ok = stream_ok
        self._snapshot = snapshot
        self.authenticated = True
        self.closed = False
        #: How many full picture captures the caller actually paid for.
        self.snapshot_fetches = 0
        self.snapshot_timeouts: list[float | None] = []
        #: Timeouts the ack-only probe was asked to honour.
        self.probe_calls: list[float] = []

    async def get_stream_url(self, channel=0):
        from episode.plugins.reolink.client import StreamUrlInfo

        if not self._stream_ok:
            raise ReolinkError("no stream")
        return StreamUrlInfo(success=True, main_stream_url="rtsp://192.168.1.10/1")

    async def get_snapshot(self, channel=0, timeout=None):
        self.snapshot_fetches += 1
        self.snapshot_timeouts.append(timeout)
        return self._snapshot

    async def snapshot_probe(self, channel=0, timeout=3.0):
        """Ack-only probe: reports support without receiving a picture."""
        from episode.plugins.reolink.client import SnapshotAck

        self.probe_calls.append(timeout)
        if self._snapshot is None:
            return None
        return SnapshotAck(response_code=0, declared_bytes=len(self._snapshot))

    async def login(self):
        return None

    async def get_device_info(self):
        return None

    @property
    def decryption_params(self):
        return {"nonce": "", "password": "", "use_aes": False, "channel": 0}

    async def close(self):
        self.closed = True


def _make_media_config(*, media_enabled=True):
    return ReolinkDeviceConfig(
        device=Device(
            id="cam-1",
            name="Front Camera",
            area_id="area-1",
            ip_address="192.168.1.10",
            username="admin",
            password="secret",
        ),
        host="192.168.1.10",
        api_port=9000,
        media_enabled=media_enabled,
    )


@pytest.mark.asyncio
async def test_connection_registers_media_when_enabled():
    registry = FakeMediaRegistry()
    client = FakeBaichuanClient()
    connection = ReolinkDeviceConnection(
        _make_media_config(media_enabled=True),
        async_noop,
        async_noop,
        media_registry=registry,
        client_factory=lambda _config: client,
    )
    await connection._discover_stream()
    source = registry.get("cam-1")
    assert source is not None
    assert source.stream_uri == "rtsp://192.168.1.10/1"
    assert source.source == "reolink"
    assert source.snapshot_fetcher is not None
    assert source.event_snapshot_fetcher is not None
    # Snapshot fetcher returns JPEG via the Reolink-native path
    data, content_type = await source.snapshot_fetcher()
    assert data[:2] == b"\xff\xd8"
    assert content_type == "image/jpeg"


@pytest.mark.asyncio
async def test_media_registry_routes_event_snapshot_tokens_separately():
    ordinary_calls = 0
    event_tokens: list[str] = []

    async def ordinary_fetch():
        nonlocal ordinary_calls
        ordinary_calls += 1
        return b"ordinary", "image/jpeg"

    async def event_fetch(token: str):
        event_tokens.append(token)
        return b"event-bound", "image/jpeg"

    registry = MediaRegistry()
    registry.register(
        CameraMedia(
            device_id="cam-1",
            source="reolink",
            snapshot_fetcher=ordinary_fetch,
            event_snapshot_fetcher=event_fetch,
        )
    )

    assert await registry.fetch_snapshot("cam-1") == (b"ordinary", "image/jpeg")
    assert await registry.fetch_snapshot("cam-1", snapshot_token="event-1") == (
        b"event-bound",
        "image/jpeg",
    )
    assert ordinary_calls == 1
    assert event_tokens == ["event-1"]


@pytest.mark.asyncio
async def test_connection_registers_native_video_handler_when_enabled():
    """``native_video`` (F1) registers the on-demand cmdId=3 handler, not just RTSP.

    The recorder must be able to choose the native source: the registered ``CameraMedia``
    carries a ``video_handler`` and a ``codec_hint`` derived from a real preview, so the
    engine's pipe branch feeds the camera's leading I-Frame to ffmpeg without a fresh RTSP
    keyframe-wait. The handler runs one on-demand burst and hands Annex-B access units to
    its consumer.
    """
    registry = FakeMediaRegistry()
    client = FakeBaichuanClient()
    config = _make_media_config(media_enabled=True)
    config = dataclasses.replace(config, preview=PreviewSettings(native_video=True, variant="main"))
    connection = ReolinkDeviceConnection(
        config,
        async_noop,
        async_noop,
        media_registry=registry,
        client_factory=lambda _config: client,
    )
    await connection._discover_stream()
    source = registry.get("cam-1")
    assert source is not None
    assert source.video_handler is not None, "native_video must register a video handler"
    assert source.codec_hint in ("h264", "hevc") or source.codec_hint == ""


@pytest.mark.asyncio
async def test_connection_does_not_register_video_handler_by_default():
    """Without ``native_video`` the media source stays RTSP-only (no handler)."""
    registry = FakeMediaRegistry()
    client = FakeBaichuanClient()
    connection = ReolinkDeviceConnection(
        _make_media_config(media_enabled=True),
        async_noop,
        async_noop,
        media_registry=registry,
        client_factory=lambda _config: client,
    )
    await connection._discover_stream()
    source = registry.get("cam-1")
    assert source is not None
    assert source.video_handler is None, "default source must remain RTSP, not native"


@pytest.mark.asyncio
async def test_connection_does_not_register_media_when_disabled():
    registry = FakeMediaRegistry()
    client = FakeBaichuanClient()
    connection = ReolinkDeviceConnection(
        _make_media_config(media_enabled=False),
        async_noop,
        async_noop,
        media_registry=registry,
        client_factory=lambda _config: client,
    )
    await connection._discover_stream()
    assert registry.get("cam-1") is None


@pytest.mark.asyncio
async def test_connection_unregisters_media_on_stop():
    registry = FakeMediaRegistry()
    client = FakeBaichuanClient()
    connection = ReolinkDeviceConnection(
        _make_media_config(media_enabled=True),
        async_noop,
        async_noop,
        media_registry=registry,
        client_factory=lambda _config: client,
    )
    await connection._discover_stream()
    assert registry.get("cam-1") is not None
    await connection._unregister_media()
    assert registry.get("cam-1") is None


@pytest.mark.asyncio
async def test_connection_skips_media_registration_without_registry():
    client = FakeBaichuanClient()
    connection = ReolinkDeviceConnection(
        _make_media_config(media_enabled=True),
        async_noop,
        async_noop,
        media_registry=None,
        client_factory=lambda _config: client,
    )
    await connection._discover_stream()
    assert connection._media_registered is False


@pytest.mark.asyncio
async def test_status_capabilities_include_media_and_snapshots_after_discovery():
    """Runtime status must report media/snapshots after stream and snapshot
    discovery, mirroring ONVIF. Otherwise device_detail overrides the stored
    validation support with a stale status that omits those capabilities."""
    client = FakeBaichuanClient()
    connection = ReolinkDeviceConnection(
        _make_media_config(media_enabled=True),
        async_noop,
        async_noop,
        client_factory=lambda _config: client,
    )
    # _discover_stream is invoked directly; _refresh_status() at its end
    # requires _discovered to be truthy to populate capabilities.
    connection._discovered = object()
    await connection._discover_stream()
    assert "discovery" in connection.status().capabilities
    assert "media" in connection.status().capabilities
    assert "snapshots" in connection.status().capabilities


@pytest.mark.asyncio
async def test_startup_probe_learns_snapshot_support_without_a_capture():
    """Gap G6: connecting must not pay for a picture the probe then throws away.

    The ack-only probe answers the capability question, so a connect costs tens of
    milliseconds instead of the 0.27-1.03 s the real captures measured, and the
    ``snapshots`` capability is still reported (it existed precisely so validation and
    runtime status agree).
    """
    client = FakeBaichuanClient()
    connection = ReolinkDeviceConnection(
        _make_media_config(media_enabled=True),
        async_noop,
        async_noop,
        client_factory=lambda _config: client,
    )
    connection._discovered = object()
    await connection._discover_stream()
    assert client.snapshot_fetches == 0, "a startup probe must not receive a picture"
    assert client.probe_calls and client.probe_calls[0] <= 3.0
    assert connection._snapshot_supported is True
    assert "snapshots" in connection.status().capabilities
    assert connection.status().details["snapshot_probe"] == {
        "declared_bytes": len(client._snapshot)
    }


@pytest.mark.asyncio
async def test_snapshot_support_is_not_re_probed_on_re_discovery():
    """A mid-life re-discovery must not pay for another probe or lose what it learned."""
    client = FakeBaichuanClient()
    connection = ReolinkDeviceConnection(
        _make_media_config(media_enabled=True),
        async_noop,
        async_noop,
        client_factory=lambda _config: client,
    )
    connection._discovered = object()
    await connection._discover_stream()
    assert client.probe_calls == [pytest.approx(3.0)]

    await connection._discover_stream()
    assert client.probe_calls == [pytest.approx(3.0)], "support was re-asked"
    assert "snapshots" in connection.status().capabilities


@pytest.mark.asyncio
async def test_snapshot_capability_survives_a_failed_capture():
    """Capability comes from the probe, so one failed capture cannot lose it.

    The probe is the capability source; a stalled camera later costs only that request.
    """
    client = FakeBaichuanClient()
    connection = ReolinkDeviceConnection(
        _make_media_config(media_enabled=True),
        async_noop,
        async_noop,
        client_factory=lambda _config: client,
    )
    connection._discovered = object()
    await connection._discover_stream()
    assert "snapshots" in connection.status().capabilities

    # A stalled or truncated capture: the camera answered once, then never again.
    client._snapshot = None
    with pytest.raises(LookupError):
        await connection._snapshot_fetcher()
    assert connection._snapshot_supported is True
    assert "snapshots" in connection.status().capabilities

    # The next request still works, and carries the configured capture timeout.
    client._snapshot = b"\xff\xd8\xff\xe0" + b"\x00" * 8 + b"\xff\xd9"
    data, content_type = await connection._snapshot_fetcher()
    assert data[:2] == b"\xff\xd8"
    assert content_type == "image/jpeg"
    assert client.snapshot_timeouts[-1] == DEFAULT_SNAPSHOT_TIMEOUT


def test_device_config_snapshot_timeout_is_bounded():
    """The setting is wired at its documented default, and an out-of-range value falls back."""
    config, error = device_config(
        _valid_device_mapping(
            configs={"reolink": {"port": 9000, "settings": {"snapshot_timeout": 2.5}}}
        )
    )
    assert error is None
    assert config is not None
    assert config.snapshot_timeout == 2.5

    fallback, error = device_config(
        _valid_device_mapping(
            configs={"reolink": {"port": 9000, "settings": {"snapshot_timeout": 999}}}
        )
    )
    assert error is None
    assert fallback is not None
    assert fallback.snapshot_timeout == DEFAULT_SNAPSHOT_TIMEOUT

    default, _ = device_config(_valid_device_mapping())
    assert default is not None
    assert default.snapshot_timeout == DEFAULT_SNAPSHOT_TIMEOUT


@pytest.mark.asyncio
async def test_status_capabilities_reflect_discovery_failure():
    """When stream discovery fails, media/snapshots must not appear in the
    runtime status capabilities."""
    client = FakeBaichuanClient(stream_ok=False, snapshot=None)
    connection = ReolinkDeviceConnection(
        _make_media_config(media_enabled=True),
        async_noop,
        async_noop,
        client_factory=lambda _config: client,
    )
    connection._discovered = object()
    await connection._discover_stream()
    assert "discovery" in connection.status().capabilities
    assert "media" not in connection.status().capabilities
    assert "snapshots" not in connection.status().capabilities


@pytest.mark.asyncio
async def test_stream_discovery_registers_conventional_rtsp_paths():
    from episode.plugins.reolink.client import BaichuanApiClient

    response = bc_encrypt(
        b"<body><StreamInfoList><StreamInfo><encodeTable>"
        b"<width>1920</width><height>1080</height>"
        b"</encodeTable></StreamInfo></StreamInfoList></body>",
        offset=250,
    )

    class Writer:
        def write(self, _data):
            pass

        async def drain(self):
            pass

    class Dispatcher:
        async def request(self, cmd_id, *, timeout, predicate=None, send=None):
            if send is not None:
                await send()
            return cmd_id, 200, 0, response

    client = BaichuanApiClient("192.168.1.10", "admin", "secret")
    client._writer = Writer()
    client._dispatcher = Dispatcher()
    result = await client._get_stream_url_impl(0, 1, "", b"", 0)

    assert result.success is True
    assert result.main_stream_url == "rtsp://192.168.1.10:554/Preview_01_main"
    assert result.sub_stream_url == "rtsp://192.168.1.10:554/Preview_01_sub"


def _make_event_config(*, events_enabled=True):
    return ReolinkDeviceConfig(
        device=Device(
            id="cam-1",
            name="Front Camera",
            area_id="area-1",
            ip_address="192.168.1.10",
            username="admin",
            password="secret",
        ),
        host="192.168.1.10",
        api_port=9000,
        events_enabled=events_enabled,
    )


@pytest.mark.asyncio
async def test_connection_starts_event_listener_when_enabled():
    client = FakeBaichuanClient()
    connection = ReolinkDeviceConnection(
        _make_event_config(events_enabled=True),
        async_noop,
        async_noop,
        client_factory=lambda _config: client,
    )
    await connection.start()
    try:
        assert connection._event_task is not None
    finally:
        await connection.stop()


@pytest.mark.asyncio
async def test_connection_does_not_start_event_listener_when_disabled():
    client = FakeBaichuanClient()
    connection = ReolinkDeviceConnection(
        _make_event_config(events_enabled=False),
        async_noop,
        async_noop,
        client_factory=lambda _config: client,
    )
    await connection.start()
    try:
        assert connection._event_task is None
    finally:
        await connection.stop()


@pytest.mark.asyncio
async def test_connection_preserves_original_frame_bytes():
    received = []

    async def capture_sink(delivery):
        received.append(delivery)

    client = FakeBaichuanClient()
    connection = ReolinkDeviceConnection(
        _make_event_config(events_enabled=True),
        capture_sink,
        async_noop,
        client_factory=lambda _config: client,
    )
    await connection.start()
    try:
        payload = b"\x00\x01encrypted-camera-frame"
        await connection._process_event_frame(33, payload, 250)
        assert len(received) == 1
        delivery = received[0]
        assert delivery.payload == payload
        assert delivery.media_type == "application/octet-stream"
        assert delivery.artifact_type == "event_frame"
        assert delivery.metadata["kind"] == "raw_event_frame"
        assert delivery.metadata["command_id"] == 33

        # Non-event telemetry frames share this ingress method but do not arm an
        # Event-bound snapshot or need a token.
        await connection._process_event_frame(252, b"telemetry", 250)
        assert len(received) == 2
        assert received[1].metadata["command_id"] == 252
        assert "snapshot_fetch_token" not in received[1].metadata
    finally:
        await connection.stop()


@pytest.mark.asyncio
async def test_snapshot_prearm_overlaps_the_ingestion_pipeline():
    """Pre-arm fires before the delivery sink unblocks, not after it.

    The delivery sink persists the raw frame and then drives the engine synchronously
    through canonicalization and video start, which takes long enough that a pre-arm
    scheduled only afterward lands the snapshot ~0.8s behind the first video frame. The
    camera's JPEG encode must instead overlap that ingestion work, so the pre-arm is armed
    before ``_delivery_sink`` is awaited — and the fetch runs while the sink is still busy.
    """
    sink_unblocked = asyncio.Event()
    prearmed_seen = asyncio.Event()

    class OrderingClient(FakeBaichuanClient):
        def __init__(self):
            super().__init__()
            self._prearm_started = False

        async def get_snapshot(self, channel: int = 0, timeout=None):
            self._prearm_started = True
            prearmed_seen.set()
            return b"\xff\xd8jpeg"

    client = OrderingClient()

    async def blocking_sink(delivery):
        # Wait until the pre-arm fetch has started before letting the pipeline return.
        await asyncio.wait_for(prearmed_seen.wait(), timeout=1.0)
        sink_unblocked.set()

    connection = ReolinkDeviceConnection(
        _make_event_config(events_enabled=True),
        blocking_sink,
        async_noop,
        client_factory=lambda _config: client,
    )
    connection._snapshot_supported = True
    await connection.start()
    try:
        await connection._process_event_frame(33, b"frame", 0)
        assert await asyncio.wait_for(sink_unblocked.wait(), timeout=1.0)
        # The pre-arm started while the delivery sink was still blocked, so the JPEG
        # encode overlaps ingestion rather than waiting for it to finish.
        assert client._prearm_started is True
    finally:
        await connection.stop()


@pytest.mark.asyncio
async def test_snapshot_prearm_arms_on_event_frame_and_serves_fetcher():
    """cmdId=33 binds a pre-armed snapshot to the derived event token."""
    received = []

    async def capture_sink(delivery):
        received.append(delivery)

    client = FakeBaichuanClient()
    connection = ReolinkDeviceConnection(
        _make_event_config(events_enabled=True),
        capture_sink,
        async_noop,
        client_factory=lambda _config: client,
    )
    connection._snapshot_supported = True
    await connection.start()
    try:
        await connection._process_event_frame(33, b"frame", 0)
        await asyncio.sleep(0)  # let the pre-arm fetch run
        assert connection._snapshot_slot.counters["armed"] == 1
        held = connection._snapshot_slot._content
        assert held is not None
        token = received[0].metadata["snapshot_fetch_token"]
        assert held[0] == token

        # Ordinary/current-view fetches must not consume an Event's pre-arm.
        fresh, fresh_type = await connection._snapshot_fetcher()
        assert fresh_type == "image/jpeg"
        assert fresh[:2] == b"\xff\xd8"
        assert connection._snapshot_slot.counters["hits"] == 0
        assert client.snapshot_fetches == 2  # pre-arm plus the ordinary fresh fetch

        data, content_type = await connection._snapshot_fetcher(token)
        assert content_type == "image/jpeg"
        # The served bytes are the pre-armed object itself: exactly one
        # cmdId=109 for the matching Event, so no second Event-bound fetch.
        assert data is held[1]
        assert data[:2] == b"\xff\xd8"
        assert connection._snapshot_slot.counters["hits"] == 1
        assert client.snapshot_fetches == 2  # matching Event consumes the pre-arm
        assert len(received) == 1  # pre-arm produced no delivery
    finally:
        await connection.stop()


@pytest.mark.asyncio
async def test_snapshot_prearm_requires_snapshot_capability():
    """Without a confirmed snapshot capability, no pre-arm may happen."""
    client = FakeBaichuanClient()
    connection = ReolinkDeviceConnection(
        _make_event_config(events_enabled=True),
        async_noop,
        async_noop,
        client_factory=lambda _config: client,
    )
    assert connection._snapshot_supported is False
    await connection.start()
    try:
        await connection._process_event_frame(33, b"frame", 0)
        await asyncio.sleep(0)
        assert connection._snapshot_slot.counters["armed"] == 0
    finally:
        await connection.stop()


@pytest.mark.asyncio
async def test_snapshot_prearm_disabled_by_setting():
    client = FakeBaichuanClient()
    config = _make_event_config(events_enabled=True)
    config = dataclasses.replace(config, snapshot_prearm=SnapshotPrearmSettings(enabled=False))
    connection = ReolinkDeviceConnection(
        config, async_noop, async_noop, client_factory=lambda _config: client
    )
    connection._snapshot_supported = True
    await connection.start()
    try:
        await connection._process_event_frame(33, b"frame", 0)
        await asyncio.sleep(0)
        assert connection._snapshot_slot.counters["armed"] == 0
        # The ordinary live fetch still works.
        data, _ = await connection._snapshot_fetcher()
        assert data[:2] == b"\xff\xd8"
    finally:
        await connection.stop()


@pytest.mark.asyncio
async def test_snapshot_prearm_expiry_falls_back_to_live_fetch():
    client = FakeBaichuanClient()
    connection = ReolinkDeviceConnection(
        _make_event_config(events_enabled=True),
        async_noop,
        async_noop,
        client_factory=lambda _config: client,
    )
    connection._snapshot_supported = True
    connection._snapshot_slot = SnapshotSlot(
        connection._fetch_snapshot,
        settings=SnapshotPrearmSettings(ttl=0.5),
    )
    token = connection._snapshot_slot.arm()
    assert token is not None
    await asyncio.sleep(0)
    assert connection._snapshot_slot.counters["armed"] == 1

    await asyncio.sleep(0.6)
    data, _ = await connection._snapshot_fetcher(token)
    assert data[:2] == b"\xff\xd8"
    assert connection._snapshot_slot.counters["expired"] == 1


@pytest.mark.asyncio
async def test_snapshot_prearm_task_is_cancelled_on_stop():
    client = FakeBaichuanClient()
    connection = ReolinkDeviceConnection(
        _make_event_config(events_enabled=True),
        async_noop,
        async_noop,
        client_factory=lambda _config: client,
    )
    connection._snapshot_supported = True
    connection._snapshot_slot.arm()
    assert connection._snapshot_slot._task is not None
    await connection.stop()
    assert connection._snapshot_slot._task is None


@pytest.mark.asyncio
async def test_connection_preserves_repeated_frames_before_deduplication():
    received = []

    async def capture_sink(delivery):
        received.append(delivery)

    client = FakeBaichuanClient()
    connection = ReolinkDeviceConnection(
        _make_event_config(events_enabled=True),
        capture_sink,
        async_noop,
        client_factory=lambda _config: client,
    )
    await connection.start()
    try:
        payload = b"<Event><cmd>MotionDetect</cmd><state>true</state></Event>"
        await connection._process_event_frame(33, payload, 0)
        await connection._process_event_frame(33, payload, 0)
        assert [delivery.payload for delivery in received] == [payload, payload]
    finally:
        await connection.stop()


@pytest.mark.asyncio
async def test_monitor_loop_is_paced_when_events_enabled():
    """The monitor loop must not spin at full speed when events are enabled,
    and must no longer poll device info periodically (capabilities are static
    and applied at startup)."""
    client = FakeBaichuanClient()
    calls = 0
    real = client.get_device_info

    async def counting_get_device_info():
        nonlocal calls
        calls += 1
        return await real()

    client.get_device_info = counting_get_device_info
    config = _make_event_config(events_enabled=True)
    config = dataclasses.replace(config, retry_delay=0.05)
    connection = ReolinkDeviceConnection(
        config,
        async_noop,
        async_noop,
        client_factory=lambda _config: client,
    )
    await connection.start()
    try:
        await asyncio.sleep(0.25)
    finally:
        await connection.stop()
    # The periodic device-info refresh was removed: get_device_info should
    # never be called by the monitor loop.
    assert calls == 0, f"monitor loop still polling device info ({calls} calls)"


# ---------------------------------------------------------------------------
# Native preview priming (cmdId=3 / cmdId=6)
# ---------------------------------------------------------------------------


class PreviewCapableClient(FakeBaichuanClient):
    """A fake client that records every native preview pass it is asked to make."""

    def __init__(self, *, preview_error: Exception | None = None, stats=None):
        super().__init__()
        self.preview_calls: list[dict] = []
        self._preview_error = preview_error
        self._preview_stats = stats if stats is not None else PreviewStats(codec="h265")

    async def subscribe_events(self, channel=0):
        return True

    @property
    def event_frame_iterator(self):
        async def _empty():
            if False:  # pragma: no cover - an empty async iterator has no body to run
                yield None

        return _empty()

    async def observe_preview_first_packet(self, channel=0, *, variant="main", timeout=None):
        self.preview_calls.append({"channel": channel, "variant": variant, "timeout": timeout})
        if self._preview_error is not None:
            raise self._preview_error
        return self._preview_stats


def _make_preview_config(
    *, priming: bool, variant: str = "main", timeout: float = 3.0, media_enabled: bool = True
):
    """Events on (the push priming reacts to) and media on (the reason to prime at all)."""
    config = _make_event_config(events_enabled=True)
    return dataclasses.replace(
        config,
        media_enabled=media_enabled,
        preview=PreviewSettings(priming=priming, variant=variant, timeout=timeout),
    )


@pytest.mark.asyncio
async def test_preview_priming_is_opt_in():
    """No ``cmdId=3`` traffic may happen unless the operator asked for it.

    Priming costs a real camera encode on the same socket the alarm pushes arrived on, and the
    plan's abort rule keeps it off until enough measured events show the first HLS fragment
    arriving sooner. So the default must be silent, and only the opt-in may produce traffic.
    """
    quiet = FakeBaichuanClient()
    silent_config = _make_preview_config(priming=False)
    assert silent_config.preview.priming is False
    silent = ReolinkDeviceConnection(
        silent_config, async_noop, async_noop, client_factory=lambda _config: quiet
    )
    silent._snapshot_supported = True
    await silent.start()
    try:
        # ``FakeBaichuanClient`` has no preview method at all, so any attempt to prime would have
        # raised inside the event loop instead of quietly doing nothing.
        await silent._process_event_frame(33, b"frame", 0)
        await asyncio.sleep(0)
    finally:
        await silent.stop()

    priming_client = PreviewCapableClient()
    priming = ReolinkDeviceConnection(
        _make_preview_config(priming=True, variant="sub", timeout=1.75),
        async_noop,
        async_noop,
        client_factory=lambda _config: priming_client,
    )
    await priming.start()
    try:
        await priming._process_event_frame(33, b"frame", 0)
        await asyncio.sleep(0)
    finally:
        await priming.stop()

    assert len(priming_client.preview_calls) == 1
    call = priming_client.preview_calls[0]
    assert call["variant"] == "sub" and call["timeout"] == 1.75


@pytest.mark.asyncio
async def test_preview_priming_requires_media_enabled():
    """Priming exists to speed up a recording, so without media there is nothing to prime.

    A camera encode on the event socket for a device that will never record is a pure cost, and
    the snapshot capability deliberately does not gate it: a camera that refuses ``cmdId=109`` can
    still stream ``cmdId=3``, and the pass reports its own failure either way.
    """
    client = PreviewCapableClient()
    connection = ReolinkDeviceConnection(
        _make_preview_config(priming=True, media_enabled=False),
        async_noop,
        async_noop,
        client_factory=lambda _config: client,
    )
    assert connection._snapshot_supported is False
    await connection.start()
    try:
        await connection._process_event_frame(33, b"frame", 0)
        await asyncio.sleep(0)
    finally:
        await connection.stop()
    assert client.preview_calls == []


@pytest.mark.asyncio
async def test_preview_priming_is_serialized_per_device():
    """A second event cannot open a second media session while the first is still running.

    Two ``cmdId=3`` passes on one socket interleave their frames into a single reassembly and pay
    the camera twice, so the connection serializes them rather than letting the camera decide.
    """
    release = asyncio.Event()

    class BlockingClient(PreviewCapableClient):
        async def observe_preview_first_packet(self, channel=0, *, variant="main", timeout=None):
            self.preview_calls.append({"channel": channel, "variant": variant})
            await release.wait()
            return PreviewStats(codec="h265")

    client = BlockingClient()
    connection = ReolinkDeviceConnection(
        _make_preview_config(priming=True), async_noop, async_noop, client_factory=lambda _c: client
    )
    await connection.start()
    try:
        for _ in range(3):
            await connection._process_event_frame(33, b"frame", 0)
            await asyncio.sleep(0)
        assert len(client.preview_calls) == 1, "a running pass is not joined by another"
        release.set()
        await asyncio.sleep(0)
        await connection._process_event_frame(33, b"frame", 0)
        await asyncio.sleep(0)
        assert len(client.preview_calls) == 2, "a finished pass does not block the next event"
    finally:
        await connection.stop()


@pytest.mark.asyncio
async def test_preview_failure_does_not_break_recording_or_snapshot():
    """A priming pass that raises is a counter and nothing else.

    Priming exists to help the *next* recording start sooner; it must never be the reason this
    event, this snapshot, or this recording fails. The pass is scheduled before the delivery
    sink so the camera warm-ups overlap ingestion, and it is never awaited, so what this test
    guards is that no exception escapes into the event loop.
    """
    client = PreviewCapableClient(preview_error=ReolinkError("camera refused preview"))
    received: list[object] = []

    async def capture_sink(delivery):
        received.append(delivery)

    connection = ReolinkDeviceConnection(
        _make_preview_config(priming=True),
        capture_sink,
        async_noop,
        client_factory=lambda _config: client,
    )
    connection._snapshot_supported = True
    await connection.start()
    try:
        await connection._process_event_frame(33, b"frame", 0)
        await asyncio.sleep(0)
        details = connection.status().details
        assert details["preview_failed_total"] == 1
        assert details["preview_observed"] == 0
        # The event itself is untouched, and the snapshot path still works.
        assert len(received) == 1
        data, content_type = await connection._snapshot_fetcher()
        assert data[:2] == b"\xff\xd8"
        assert content_type == "image/jpeg"
    finally:
        await connection.stop()


@pytest.mark.asyncio
async def test_preview_priming_counters_and_last_measurement_are_reported():
    """The numbers the plan's abort rule is settled with are reported flat and named as such.

    ``preview_timeout_total`` is the counter that decides whether priming stays: a pass that asked
    for a keyframe and got nothing is exactly one increment of it, distinct from a pass that
    raised. Reporting them under other names would make the abort rule unauditable.
    """
    # Timings are reported rounded to a tenth of a millisecond: a status payload that reproduced
    # every digit of a loop timer would suggest a precision it does not have.
    observed = PreviewStats(
        codec="h265",
        width=3840,
        height=2160,
        info_seen=True,
        iframe_count=1,
        sps=1,
        pps=1,
        irap=1,
        first_packet_ms=12.5,
        first_iframe_ms=20.256,
    )
    empty = PreviewStats()

    class TwoPassClient(PreviewCapableClient):
        def __init__(self):
            super().__init__()
            self._results = [observed, empty]

        async def observe_preview_first_packet(self, channel=0, *, variant="main", timeout=None):
            self.preview_calls.append({"channel": channel, "variant": variant})
            return self._results[len(self.preview_calls) - 1]

    client = TwoPassClient()
    connection = ReolinkDeviceConnection(
        _make_preview_config(priming=True), async_noop, async_noop, client_factory=lambda _c: client
    )
    await connection.start()
    try:
        for _ in range(2):
            await connection._process_event_frame(33, b"frame", 0)
            await asyncio.sleep(0)
        details = connection.status().details
        assert details["preview_attempted"] == 2
        assert details["preview_observed"] == 2
        assert details["preview_failed_total"] == 0
        assert details["preview_timeout_total"] == 1, "one pass asked for a keyframe and got none"
        assert details["preview_codec"] == "" and details["preview_first_packet_ms"] is None
    finally:
        await connection.stop()

    client2 = PreviewCapableClient(stats=observed)
    connection2 = ReolinkDeviceConnection(
        _make_preview_config(priming=True),
        async_noop,
        async_noop,
        client_factory=lambda _c: client2,
    )
    await connection2.start()
    try:
        await connection2._process_event_frame(33, b"frame", 0)
        await asyncio.sleep(0)
        details = connection2.status().details
        assert details["preview_codec"] == "h265"
        assert details["preview_first_packet_ms"] == 12.5
        assert details["preview_first_iframe_ms"] == 20.3
    finally:
        await connection2.stop()


@pytest.mark.asyncio
async def test_preview_priming_task_is_cancelled_on_stop():
    """No priming pass may outlive the connection it was started for."""
    release = asyncio.Event()

    class BlockingClient(PreviewCapableClient):
        async def observe_preview_first_packet(self, channel=0, *, variant="main", timeout=None):
            self.preview_calls.append({"channel": channel, "variant": variant})
            await release.wait()
            return PreviewStats()

    client = BlockingClient()
    connection = ReolinkDeviceConnection(
        _make_preview_config(priming=True), async_noop, async_noop, client_factory=lambda _c: client
    )
    await connection.start()
    await connection._process_event_frame(33, b"frame", 0)
    await asyncio.sleep(0)
    assert connection._preview_task is not None
    await asyncio.wait_for(connection.stop(), 2.0)
    assert connection._preview_task is None


def test_device_config_preview_settings_are_wired_at_their_defaults():
    """``media_priming`` / ``preview_variant`` / ``preview_timeout`` reach the device config.

    Parsing them and dropping them on the way into ``ReolinkDeviceConfig`` is a bug no socket test
    can see, because the socket tests pass their settings explicitly.
    """
    config, error = device_config(_valid_device_mapping())
    assert error is None
    assert config is not None
    assert config.preview.priming is False, "priming is off until an operator opts in"
    assert config.preview.variant == "main"
    assert config.preview.timeout == DEFAULT_PREVIEW_TIMEOUT

    config, error = device_config(
        _valid_device_mapping(
            configs={
                "reolink": {
                    "port": 9000,
                    "settings": {
                        "media_priming": "true",
                        "preview_variant": "sub",
                        "preview_timeout": 2.5,
                    },
                }
            }
        )
    )
    assert error is None
    assert config is not None
    assert config.preview.priming is True
    assert config.preview.variant == "sub"
    assert config.preview.timeout == 2.5


def test_device_config_preview_settings_fall_back_without_failing():
    """A bad preview value warns and defaults; it must not stop a device from connecting."""
    config, error = device_config(
        _valid_device_mapping(
            configs={
                "reolink": {
                    "port": 9000,
                    "settings": {"media_priming": "maybe", "preview_variant": "third"},
                }
            }
        )
    )
    assert error is None
    assert config is not None
    assert config.preview.priming is False
    assert config.preview.variant == "main"

    config, error = device_config(
        _valid_device_mapping(
            configs={"reolink": {"port": 9000, "settings": {"preview_timeout": 999}}}
        )
    )
    assert error is None
    assert config is not None
    assert config.preview.timeout == DEFAULT_PREVIEW_TIMEOUT


# ---------------------------------------------------------------------------
# Plugin lifecycle
# ---------------------------------------------------------------------------


class FakeRouter:
    def __init__(self):
        self.registrations = []
        self.unregistered = []

    def register(self, registration):
        self.registrations.append(registration)

    def unregister(self, handler_id):
        self.unregistered.append(handler_id)

    def status(self, handler_id):
        return {"handler": handler_id, "count": 1}


class FakeConnection:
    def __init__(self, device_id, name):
        self._status = PluginInstanceStatus(
            id=device_id,
            name=name,
            state=PluginInstanceState.RUNNING,
        )
        self.started = False
        self.stopped = False

    def status(self):
        return self._status

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


def _make_plugin(connections=None):
    router = FakeRouter()
    context = PluginContext(
        plugins_dir="plugins",
        configured_devices=(_valid_device_mapping(),),
        ingress_router=router,
        raw_delivery_sink=async_noop,
        device_update_sink=async_noop,
    )
    factory = (lambda _c, _d, _u, **_kw: next(iter(connections))) if connections else _stub_factory
    return ReolinkPlugin(context, connection_factory=factory)


def _stub_factory(_config, _delivery, _update, **_kwargs):
    return FakeConnection("cam-1", "Front Camera")


async def async_noop(*_args, **_kwargs):
    return None


@pytest.mark.asyncio
async def test_plugin_start_registers_handler_and_creates_connections():
    plugin = _make_plugin()
    await plugin.start()
    assert plugin._registered is True
    assert any(r.id == "reolink-events" for r in plugin._router.registrations)
    assert len(plugin._connections) == 1
    assert plugin._connections[0].started is True


@pytest.mark.asyncio
async def test_plugin_stop_cleans_up():
    plugin = _make_plugin()
    await plugin.start()
    await plugin.stop()
    assert plugin._registered is False
    assert "reolink-events" in plugin._router.unregistered
    assert plugin._connections == []


def _envelope(*, device_id="cam-1", event_type="motion_detection", event_state="active"):
    from episode.ingestion.models import StoredIngressEnvelope

    return StoredIngressEnvelope(
        receipt_id="rec-1",
        artifact_id="art-1",
        source="reolink:events",
        transport="plugin",
        received_at=datetime.now(tz=timezone.utc),
        payload=b"{}",
        media_type="application/json",
        device_id=device_id,
        area_id="area-1",
        metadata={
            "plugin_id": "reolink",
            "kind": "event_notification",
            "event_type": event_type,
            "event_state": event_state,
            "channel": 0,
            "event_id": "evt-1",
        },
    )


@pytest.mark.asyncio
async def test_handler_preserves_then_expands_raw_frame():
    from episode.ingestion.models import ReceiptStatus, StoredIngressEnvelope

    derived = []

    async def capture(delivery):
        derived.append(delivery)

    plugin = ReolinkPlugin(
        PluginContext(
            plugins_dir="plugins",
            configured_devices=(_valid_device_mapping(),),
            ingress_router=FakeRouter(),
            raw_delivery_sink=capture,
            device_update_sink=async_noop,
        )
    )
    connection = ReolinkDeviceConnection(
        _make_event_config(events_enabled=True),
        capture,
        async_noop,
        client_factory=lambda _config: FakeBaichuanClient(),
    )
    plugin._connections = [connection]
    raw = StoredIngressEnvelope(
        receipt_id="raw-receipt",
        artifact_id="raw-artifact",
        source="reolink:events",
        transport="plugin",
        received_at=datetime.now(tz=timezone.utc),
        payload=b"<Event><cmd>PersonDetect</cmd><state>true</state></Event>",
        media_type="application/octet-stream",
        device_id="cam-1",
        area_id="area-1",
        metadata={
            "plugin_id": "reolink",
            "kind": "raw_event_frame",
            "command_id": 33,
            "channel": 0,
            "nonce": "",
            "use_aes": False,
            "snapshot_fetch_token": "event-token",
        },
    )

    expanded = await plugin._handle(raw)

    assert expanded.status == ReceiptStatus.IGNORED
    assert expanded.metadata["notification_count"] == 1
    assert len(derived) == 1
    assert derived[0].artifact_type == "derived_event_notification"
    assert derived[0].metadata["parent_receipt_id"] == "raw-receipt"
    assert derived[0].metadata["snapshot_fetch_token"] == "event-token"

    notification = dataclasses.replace(
        raw,
        receipt_id="derived-receipt",
        artifact_id="derived-artifact",
        payload=derived[0].payload,
        media_type=derived[0].media_type,
        metadata={"plugin_id": "reolink", **dict(derived[0].metadata)},
    )
    interpreted = await plugin._handle(notification)
    assert interpreted.event is not None
    assert interpreted.event.event_type == "human_detection"
    assert interpreted.event.metadata["snapshot_fetch_token"] == "event-token"


@pytest.mark.asyncio
async def test_handler_keeps_battery_telemetry_out_of_episode_events():
    from episode.domain.models import ReceiptStatus

    plugin = _make_plugin()
    connection = FakeConnection("cam-1", "Front Camera")
    connection.config = _make_event_config(events_enabled=True)
    plugin._connections = [connection]
    raw = dataclasses.replace(
        _envelope(),
        payload=b"<batteryStatus><batteryPower>12</batteryPower></batteryStatus>",
        media_type="application/octet-stream",
        metadata={
            "plugin_id": "reolink",
            "kind": "raw_event_frame",
            "command_id": 252,
            "channel": 0,
        },
    )

    result = await plugin._handle(raw)

    assert result.status == ReceiptStatus.IGNORED
    assert result.metadata == {"reason": "device_telemetry", "command_id": 252}


@pytest.mark.asyncio
async def test_handler_registers_event_on_transition():
    """A new event state must be interpreted into an EventObservation."""
    from episode.ingestion.models import EventObservation

    plugin = _make_plugin()
    result = await plugin._handle(_envelope(event_type="motion_detection", event_state="active"))
    assert result.claimed is True
    assert result.event is not None
    assert isinstance(result.event, EventObservation)
    assert result.event.event_type == "motion_detection"
    assert result.event.event_state == "active"
    assert result.event.device_id == "cam-1"
    assert result.event.source == "reolink:events"


@pytest.mark.asyncio
async def test_handler_suppresses_repeated_state():
    """Repeated identical states must be ignored, not re-registered."""
    from episode.ingestion.models import ReceiptStatus

    plugin = _make_plugin()
    first = await plugin._handle(_envelope(event_type="motion_detection", event_state="active"))
    assert first.event is not None
    second = await plugin._handle(_envelope(event_type="motion_detection", event_state="active"))
    assert second.event is None
    assert second.status == ReceiptStatus.IGNORED
    assert plugin._suppressed_counts.get("cam-1", 0) == 1


@pytest.mark.asyncio
async def test_handler_emits_on_state_change():
    """A transition from active to inactive must register a new event."""
    plugin = _make_plugin()
    first = await plugin._handle(_envelope(event_type="motion_detection", event_state="active"))
    assert first.event is not None
    second = await plugin._handle(_envelope(event_type="motion_detection", event_state="inactive"))
    assert second.event is not None
    assert second.event.event_state == "inactive"


@pytest.mark.asyncio
async def test_handler_registers_same_state_after_window():
    """A repeated state after the dedup window must be registered again."""
    from datetime import timedelta

    plugin = _make_plugin()
    first = await plugin._handle(_envelope(event_type="motion_detection", event_state="active"))
    assert first.event is not None

    env = _envelope(event_type="motion_detection", event_state="active")
    later = env.received_at + timedelta(seconds=10)
    env = dataclasses.replace(env, received_at=later, receipt_id="rec-2")
    second = await plugin._handle(env)
    assert second.event is not None
    assert second.event.event_state == "active"


@pytest.mark.asyncio
async def test_handler_honours_configured_dedup_window():
    """The configured dedup_window must be the effective suppression window."""
    from datetime import timedelta

    device = _valid_device_mapping()
    device["configs"]["reolink"]["settings"]["dedup_window"] = 8.0
    context = PluginContext(
        plugins_dir="plugins",
        configured_devices=(device,),
        ingress_router=FakeRouter(),
        raw_delivery_sink=async_noop,
        device_update_sink=async_noop,
    )
    plugin = ReolinkPlugin(context, connection_factory=_stub_factory)
    assert plugin._dedup_windows == {"cam-1": 8.0}

    first = await plugin._handle(_envelope(event_type="motion_detection", event_state="active"))
    assert first.event is not None

    mid = _envelope(event_type="motion_detection", event_state="active")
    mid = dataclasses.replace(mid, received_at=mid.received_at + timedelta(seconds=4))
    assert (await plugin._handle(mid)).event is None  # inside the configured 8 s window

    later = _envelope(event_type="motion_detection", event_state="active")
    later = dataclasses.replace(later, received_at=later.received_at + timedelta(seconds=12))
    assert (await plugin._handle(later)).event is not None


def test_handler_dedup_window_defaults_to_documented_value():
    """The documented default must equal the effective default."""
    assert _make_plugin()._dedup_windows == {"cam-1": DEFAULT_DEDUP_WINDOW}


@pytest.mark.asyncio
async def test_handler_rejects_missing_fields():
    from episode.ingestion.models import ReceiptStatus

    plugin = _make_plugin()
    env = _envelope()
    env = dataclasses.replace(
        env,
        metadata={"plugin_id": "reolink", "kind": "event_notification"},
    )
    result = await plugin._handle(env)
    assert result.status == ReceiptStatus.REJECTED


# ---------------------------------------------------------------------------
# Status aggregation
# ---------------------------------------------------------------------------


def test_status_ready():
    plugin = ReolinkPlugin(
        PluginContext(
            plugins_dir="plugins",
            configured_devices=(_valid_device_mapping(),),
            ingress_router=FakeRouter(),
            raw_delivery_sink=async_noop,
            device_update_sink=async_noop,
        )
    )
    plugin._registered = True
    plugin._connections = [FakeConnection("cam-1", "Front Camera")]
    status = plugin.status()
    assert status.state == PluginState.READY
    assert status.error is None


def test_status_degraded():
    plugin = ReolinkPlugin(
        PluginContext(
            plugins_dir="plugins",
            configured_devices=(_valid_device_mapping(),),
            ingress_router=FakeRouter(),
            raw_delivery_sink=async_noop,
            device_update_sink=async_noop,
        )
    )
    plugin._registered = True
    failing = FakeConnection("cam-1", "Front Camera")
    failing._status = PluginInstanceStatus(
        id="cam-1",
        name="Front Camera",
        state=PluginInstanceState.FAILED,
        error="Auth failed",
    )
    plugin._connections = [failing, FakeConnection("cam-2", "Back Camera")]
    status = plugin.status()
    assert status.state == PluginState.DEGRADED


def test_status_failed_when_all_connections_failed():
    plugin = ReolinkPlugin(
        PluginContext(
            plugins_dir="plugins",
            configured_devices=(_valid_device_mapping(),),
            ingress_router=FakeRouter(),
            raw_delivery_sink=async_noop,
            device_update_sink=async_noop,
        )
    )
    plugin._registered = True
    failing = FakeConnection("cam-1", "Front Camera")
    failing._status = PluginInstanceStatus(
        id="cam-1",
        name="Front Camera",
        state=PluginInstanceState.FAILED,
        error="Auth failed",
    )
    plugin._connections = [failing]
    status = plugin.status()
    assert status.state == PluginState.FAILED


def test_status_failed_without_router_registration():
    plugin = ReolinkPlugin(
        PluginContext(
            plugins_dir="plugins",
            configured_devices=(_valid_device_mapping(),),
            ingress_router=FakeRouter(),
            raw_delivery_sink=async_noop,
            device_update_sink=async_noop,
        )
    )
    status = plugin.status()
    assert status.state == PluginState.FAILED


# ---------------------------------------------------------------------------
# Registry selection
# ---------------------------------------------------------------------------


def test_registry_selects_reolink_for_configuration():
    registry = builtin_plugin_registry()
    plugins = registry.for_configuration({"reolink"})
    assert any(p.id == "reolink" for p in plugins)


def test_registry_has_reolink_validation_capability():
    registry = builtin_plugin_registry()
    validators = registry.validators()
    assert "reolink" in validators


# ---------------------------------------------------------------------------
# validate_device result mapping
# ---------------------------------------------------------------------------


class FakeClient:
    def __init__(self, outcome):
        self._outcome = outcome
        self.closed = False

    async def login(self):
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome

    async def close(self):
        self.closed = True


class FakeDeviceInfo:
    def __init__(self):
        self.mac_address = "AA:BB:CC"
        self.model = "Reolink RLC-810A"
        self.firmware_version = "v3.1.0"
        self.channel_count = 1


def _make_validation_device():
    from episode.domain.models import Device

    return Device(
        id="cam-1",
        name="Front Camera",
        device_type="camera",
        area_id="area-1",
        ip_address="192.168.1.10",
        username="admin",
        password="secret",
        configs={"reolink": CapabilityConfig(port=9000, settings={"host": ""})},
    )


def test_validate_device_supported(monkeypatch):
    from episode.plugins.reolink import validation as validation_module

    async def fake_login(_self):
        return FakeDeviceInfo()

    monkeypatch.setattr(
        validation_module.BaichuanApiClient,
        "login",
        fake_login,
    )

    async def run():
        return await validation_module.validate_device(
            _make_validation_device(), "2026-01-01T00:00:00Z", 5.0
        )

    result = asyncio.run(run())
    assert result["status"] == "supported"
    assert result["details"]["model"] == "Reolink RLC-810A"


def test_validate_device_auth_failed(monkeypatch):
    from episode.plugins.reolink import validation as validation_module
    from episode.plugins.reolink.client import ReolinkLoginError

    async def fake_login(_self):
        raise ReolinkLoginError("bad credentials")

    monkeypatch.setattr(
        validation_module.BaichuanApiClient,
        "login",
        fake_login,
    )

    async def run():
        return await validation_module.validate_device(
            _make_validation_device(), "2026-01-01T00:00:00Z", 5.0
        )

    result = asyncio.run(run())
    assert result["status"] == "authentication_failed"


def test_validate_device_closes_client_after_failure(monkeypatch):
    from episode.plugins.reolink import validation as validation_module
    from episode.plugins.reolink.client import ReolinkLoginError

    client = FakeClient(ReolinkLoginError("bad credentials"))
    monkeypatch.setattr(validation_module, "BaichuanApiClient", lambda **_kwargs: client)

    result = asyncio.run(
        validation_module.validate_device(_make_validation_device(), "2026-01-01T00:00:00Z", 5.0)
    )

    assert result["status"] == "authentication_failed"
    assert client.closed is True


def test_validate_device_unavailable(monkeypatch):
    from episode.plugins.reolink import validation as validation_module
    from episode.plugins.reolink.client import ReolinkError

    async def fake_login(_self):
        raise ReolinkError("timeout")

    monkeypatch.setattr(
        validation_module.BaichuanApiClient,
        "login",
        fake_login,
    )

    async def run():
        return await validation_module.validate_device(
            _make_validation_device(), "2026-01-01T00:00:00Z", 5.0
        )

    result = asyncio.run(run())
    assert result["status"] == "unavailable"


class FakeStreamUrlInfo:
    def __init__(self, success=True, streams=None, main_stream_url=""):
        self.success = success
        self.streams = streams or [{"encodeTables": [{"width": 3840, "height": 2160}]}]
        self.main_stream_url = main_stream_url
        self.error = ""


def test_validate_device_reports_full_capabilities(monkeypatch):
    """Validation should probe and report media, events and snapshots.

    Snapshot support is decided by the ``cmdId=109`` acknowledgment alone, so validation
    never receives a picture it would throw away (gap G6).
    """
    from episode.plugins.reolink import validation as validation_module
    from episode.plugins.reolink.client import SnapshotAck

    async def fake_login(_self):
        return FakeDeviceInfo()

    async def fake_get_stream_url(_self, channel=0):
        return FakeStreamUrlInfo(success=True, main_stream_url="rtsp://192.168.1.10/1")

    async def fake_subscribe_events(_self):
        return True

    async def fake_snapshot_probe(_self, channel=0, timeout=3.0):
        return SnapshotAck(response_code=0, declared_bytes=3791559)

    async def fake_get_snapshot(_self, channel=0, timeout=None):
        raise AssertionError("validation must not capture a snapshot")

    monkeypatch.setattr(validation_module.BaichuanApiClient, "login", fake_login)
    monkeypatch.setattr(validation_module.BaichuanApiClient, "get_stream_url", fake_get_stream_url)
    monkeypatch.setattr(
        validation_module.BaichuanApiClient, "subscribe_events", fake_subscribe_events
    )
    monkeypatch.setattr(validation_module.BaichuanApiClient, "snapshot_probe", fake_snapshot_probe)
    monkeypatch.setattr(validation_module.BaichuanApiClient, "get_snapshot", fake_get_snapshot)

    async def run():
        return await validation_module.validate_device(
            _make_validation_device(), "2026-01-01T00:00:00Z", 5.0
        )

    result = asyncio.run(run())
    assert result["status"] == "supported"
    caps = result["capabilities"]
    assert "discovery" in caps
    assert "media" in caps
    assert "events" in caps
    assert "snapshots" in caps
    assert result["details"]["streams"] == 1
    assert result["details"]["stream_supported"] is True
    assert result["details"]["events_supported"] is True
    assert result["details"]["snapshot_declared_bytes"] == 3791559


def test_validate_device_probe_failures_keep_discovery(monkeypatch):
    """When all probes fail, validation still succeeds with discovery only."""
    from episode.plugins.reolink import validation as validation_module
    from episode.plugins.reolink.client import ReolinkError

    async def fake_login(_self):
        return FakeDeviceInfo()

    async def fake_get_stream_url(_self, channel=0):
        raise ReolinkError("stream probe failed")

    async def fake_subscribe_events(_self):
        raise ReolinkError("event subscription probe failed")

    async def fake_snapshot_probe(_self, channel=0, timeout=3.0):
        raise ReolinkError("snapshot probe failed")

    monkeypatch.setattr(validation_module.BaichuanApiClient, "login", fake_login)
    monkeypatch.setattr(validation_module.BaichuanApiClient, "get_stream_url", fake_get_stream_url)
    monkeypatch.setattr(
        validation_module.BaichuanApiClient, "subscribe_events", fake_subscribe_events
    )
    monkeypatch.setattr(validation_module.BaichuanApiClient, "snapshot_probe", fake_snapshot_probe)

    async def run():
        return await validation_module.validate_device(
            _make_validation_device(), "2026-01-01T00:00:00Z", 5.0
        )

    result = asyncio.run(run())
    assert result["status"] == "supported"
    assert result["capabilities"] == ["discovery"]
    assert result["details"]["events_supported"] is False
    assert result["details"]["snapshot_declared_bytes"] is None


def test_validate_device_reports_media_without_events_or_snapshots(monkeypatch):
    """Streams present but no events/snapshots -> only media appended."""
    from episode.plugins.reolink import validation as validation_module

    async def fake_login(_self):
        return FakeDeviceInfo()

    async def fake_get_stream_url(_self, channel=0):
        return FakeStreamUrlInfo(success=True, main_stream_url="rtsp://192.168.1.10/1")

    async def fake_subscribe_events(_self):
        return False

    async def fake_snapshot_probe(_self, channel=0, timeout=3.0):
        return None  # camera never acknowledged the snapshot request

    monkeypatch.setattr(validation_module.BaichuanApiClient, "login", fake_login)
    monkeypatch.setattr(validation_module.BaichuanApiClient, "get_stream_url", fake_get_stream_url)
    monkeypatch.setattr(
        validation_module.BaichuanApiClient, "subscribe_events", fake_subscribe_events
    )
    monkeypatch.setattr(validation_module.BaichuanApiClient, "snapshot_probe", fake_snapshot_probe)

    async def run():
        return await validation_module.validate_device(
            _make_validation_device(), "2026-01-01T00:00:00Z", 5.0
        )

    result = asyncio.run(run())
    caps = result["capabilities"]
    assert "media" in caps
    assert "events" not in caps
    assert "snapshots" not in caps


# ── Subscribe / keepalive tests ─────────────────────────────────────────


class _FakeWriter:
    def __init__(self):
        self.data = []

    def write(self, data: bytes) -> None:
        self.data.append(data)

    async def drain(self) -> None:
        pass


def _authenticated_client(monkeypatch, response_codes):
    """Build a BaichuanApiClient that is 'authenticated' with a mocked
    dispatcher whose request() returns the given response codes in order."""
    from episode.plugins.reolink.client import BaichuanApiClient

    client = BaichuanApiClient("192.168.1.10", "admin", "pw", api_port=9000, timeout=5.0)
    client._token = "tok"
    client._connected = True
    client._writer = _FakeWriter()

    calls = []

    class _Dispatcher:
        async def request(self, cmd_id, *, timeout, predicate=None, send=None):
            calls.append((cmd_id, send))
            if send is not None:
                await send()
            code = response_codes[min(len(calls) - 1, len(response_codes) - 1)]
            return cmd_id, code, 0, b""

    client._dispatcher = _Dispatcher()
    return client, calls


def test_subscribe_events_sends_cmd31_and_returns_true(monkeypatch):
    client, calls = _authenticated_client(monkeypatch, [200])
    assert asyncio.run(client.subscribe_events()) is True
    # At least one cmdId=31 frame was sent (the first channel variant).
    assert any(cmd == 31 for cmd, _ in calls)


def test_subscribe_events_retries_channels_and_returns_false(monkeypatch):
    client, calls = _authenticated_client(monkeypatch, [421, 421, 421])
    assert asyncio.run(client.subscribe_events()) is False
    # All three channel variants (0, 251, 250) were attempted.
    assert len(calls) >= 3
    assert all(cmd == 31 for cmd, _ in calls)


def test_subscribe_events_accepts_second_channel(monkeypatch):
    client, calls = _authenticated_client(monkeypatch, [421, 200])
    assert asyncio.run(client.subscribe_events()) is True


def test_subscribe_events_requires_authentication():
    from episode.plugins.reolink.client import BaichuanApiClient, ReolinkError

    client = BaichuanApiClient("192.168.1.10", "admin", "pw")
    with pytest.raises(ReolinkError):
        asyncio.run(client.subscribe_events())


def test_ping_sends_cmd93_and_reports_success(monkeypatch):
    client, calls = _authenticated_client(monkeypatch, [200])
    assert asyncio.run(client.ping()) is True
    assert all(cmd == 93 for cmd, _ in calls)
