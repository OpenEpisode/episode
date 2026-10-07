from __future__ import annotations

import asyncio
import base64
import ctypes
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from episode.config import EpisodeConfig
from episode.domain.models import Area, Device
from episode.engine.bus import EventBus
from episode.engine.engine import EpisodeEngine
from episode.ingestion.router import IngressHandlerRegistration, IngressRouter
from episode.ingestion.service import IngestionService
from episode.plugins.deliveries import RawPluginDeliveryStore
from episode.plugins.hikvision.sdk.events import ANTI_TAMPER
from episode.plugins.hikvision.sdk.plugin import HikvisionSDKPlugin
from episode.plugins.hikvision.sdk.runtime import SDKDeviceConfig, SDKDeviceWorker
from episode.plugins.hikvision.sdk.worker import (
    MAX_SNAPSHOT_BYTES,
    NET_DVR_ALARMER,
    NET_DVR_DEVICECFG_V50,
    NET_DVR_DEVICEINFO_V30,
    NET_DVR_DEVICEINFO_V40,
    NET_DVR_FIRMWARE_VERSION_INFO,
    NET_DVR_GET_DEVICECFG_V50,
    NET_DVR_GET_FIRMWARE_VERSION,
    NET_DVR_JPEGPARA,
    NET_DVR_SETUPALARM_PARAM_V50,
    NET_DVR_USER_LOGIN_INFO,
    WORKER_MESSAGE_PREFIX,
    SnapshotCaptureError,
    _capture_jpeg,
    _format_firmware_version,
    _get_device_info,
)
from episode.plugins.models import PluginInstanceState, RawPluginDelivery
from episode.storage.repository import Repository


def _config() -> SDKDeviceConfig:
    return SDKDeviceConfig(
        id="front-doorbell",
        name="Front Doorbell",
        area_id="front-door",
        address="192.0.2.10",
        port=8000,
        username="sdk-user",
        password="not-exposed",
    )


def _ring_payload() -> bytes:
    payload = bytearray(560)
    payload[:4] = len(payload).to_bytes(4, "little")
    payload[4:6] = (2026).to_bytes(2, "little")
    payload[6:11] = bytes((8, 3, 12, 33, 17))
    payload[44] = 17
    return bytes(payload)


def _tamper_payload() -> bytes:
    payload = bytearray(_ring_payload())
    payload[44] = ANTI_TAMPER
    return bytes(payload)


def _worker_source(*messages: dict, wait: bool = False) -> str:
    encoded = [
        WORKER_MESSAGE_PREFIX + json.dumps(message, separators=(",", ":")) for message in messages
    ]
    lines = [
        "import json, sys, time",
        "json.loads(sys.stdin.readline())",
        *(f"print({line!r}, flush=True)" for line in encoded),
    ]
    if wait:
        lines.append("time.sleep(60)")
    return ";".join(lines)


def _snapshot_worker_source(
    payload: bytes,
    *,
    respond: bool = True,
    error: bool = False,
) -> str:
    encoded = base64.b64encode(payload).decode()
    ready = WORKER_MESSAGE_PREFIX + json.dumps({"type": "ready"}, separators=(",", ":"))
    prefix = WORKER_MESSAGE_PREFIX
    response = (
        {
            "type": "snapshot_error",
            "message": "capture rejected",
            "code": 123,
        }
        if error
        else {
            "type": "snapshot",
            "length": len(payload),
            "content_type": "image/jpeg",
            "payload": encoded,
        }
    )
    lines = [
        "import json, sys",
        "json.loads(sys.stdin.readline())",
        f"print({ready!r}, flush=True)",
        "for line in sys.stdin:",
        "    command = json.loads(line)",
    ]
    if respond:
        lines.extend(
            [
                f"    response = {response!r}",
                '    response["request_id"] = command["request_id"]',
                f"    print({prefix!r} + json.dumps(response, separators=(',', ':')), flush=True)",
            ]
        )
    return "\n".join(lines)


@pytest.mark.asyncio
async def test_worker_preserves_raw_notification_and_reports_health(tmp_path):
    deliveries: list[RawPluginDelivery] = []

    async def preserve(delivery: RawPluginDelivery) -> None:
        deliveries.append(delivery)

    payload = _ring_payload()
    received_at = datetime.now(tz=timezone.utc).isoformat()
    command = [
        sys.executable,
        "-c",
        _worker_source(
            {"type": "ready"},
            {
                "type": "alarm",
                "command": 0x1133,
                "length": len(payload),
                "received_at": received_at,
                "payload": base64.b64encode(payload).decode(),
            },
            wait=True,
        ),
    ]
    worker = SDKDeviceWorker(tmp_path, _config(), preserve, command=command)

    assert await worker.start()
    for _attempt in range(100):
        if deliveries:
            break
        await asyncio.sleep(0.001)

    assert len(deliveries) == 1
    assert deliveries[0].payload == payload
    assert deliveries[0].metadata == {
        "command": 0x1133,
        "sdk_buffer_length": len(payload),
    }
    status = worker.status()
    assert status.state == PluginInstanceState.RUNNING
    assert status.capabilities == ("events",)
    assert status.messages_received == 1
    assert status.last_message_at is not None
    assert "not-exposed" not in repr(status)

    await worker.stop()
    assert worker.status().state == PluginInstanceState.STOPPED


@pytest.mark.asyncio
async def test_worker_reports_validated_device_information(tmp_path):
    async def preserve(_delivery: RawPluginDelivery) -> None:
        pass

    command = [
        sys.executable,
        "-c",
        _worker_source(
            {
                "type": "ready",
                "snapshot_supported": True,
                "device_info": {
                    "manufacturer": "Hikvision",
                    "model": "DS-KV8113-WME1",
                    "firmware_version": "V3.6.0 build 250522",
                },
            },
            wait=True,
        ),
    ]
    worker = SDKDeviceWorker(tmp_path, _config(), preserve, command=command)

    assert await worker.start()

    info = worker.status().device_info
    assert info is not None
    assert info.manufacturer == "Hikvision"
    assert info.model == "DS-KV8113-WME1"
    assert info.firmware_version == "V3.6.0 build 250522"
    assert worker.status().capabilities == ("events", "snapshot")
    await worker.stop()


@pytest.mark.asyncio
async def test_worker_requests_snapshot_from_existing_process(tmp_path):
    async def preserve(_delivery: RawPluginDelivery) -> None:
        pass

    payload = b"\xff\xd8sdk-jpeg\xff\xd9"
    command = [
        sys.executable,
        "-c",
        _snapshot_worker_source(payload),
    ]
    worker = SDKDeviceWorker(tmp_path, _config(), preserve, command=command)

    assert await worker.start()
    process = worker._process
    captured, content_type = await worker.capture_snapshot()

    assert captured == payload
    assert content_type == "image/jpeg"
    await worker.stop()
    assert process is not None and process.stdin is not None and process.stdin.is_closing()


@pytest.mark.asyncio
async def test_worker_snapshot_timeout_does_not_fail_worker(tmp_path):
    async def preserve(_delivery: RawPluginDelivery) -> None:
        pass

    command = [
        sys.executable,
        "-c",
        _snapshot_worker_source(b"\xff\xd8unused\xff\xd9", respond=False),
    ]
    worker = SDKDeviceWorker(
        tmp_path,
        _config(),
        preserve,
        command=command,
        snapshot_timeout=0.02,
    )

    assert await worker.start()
    with pytest.raises(TimeoutError, match="snapshot request timed out"):
        await worker.capture_snapshot()
    assert worker.status().state == PluginInstanceState.RUNNING
    await worker.stop()


@pytest.mark.asyncio
async def test_worker_snapshot_error_is_scoped_to_request(tmp_path):
    async def preserve(_delivery: RawPluginDelivery) -> None:
        pass

    command = [
        sys.executable,
        "-c",
        _snapshot_worker_source(b"\xff\xd8unused\xff\xd9", error=True),
    ]
    worker = SDKDeviceWorker(tmp_path, _config(), preserve, command=command)

    assert await worker.start()
    with pytest.raises(RuntimeError, match="capture rejected.*123"):
        await worker.capture_snapshot()
    assert worker.status().state == PluginInstanceState.RUNNING
    await worker.stop()


@pytest.mark.asyncio
async def test_worker_rejects_invalid_snapshot_response(tmp_path):
    async def preserve(_delivery: RawPluginDelivery) -> None:
        pass

    command = [
        sys.executable,
        "-c",
        _snapshot_worker_source(b"not-a-jpeg"),
    ]
    worker = SDKDeviceWorker(tmp_path, _config(), preserve, command=command)

    assert await worker.start()
    with pytest.raises(RuntimeError, match="invalid JPEG"):
        await worker.capture_snapshot()
    assert worker.status().state == PluginInstanceState.RUNNING
    await worker.stop()


def test_worker_queries_friendly_model_and_firmware_from_sdk():
    class FakeSDK:
        @staticmethod
        def NET_DVR_GetDVRConfig(  # noqa: N802 - mirrors the vendor function
            _user_id, command, _channel, output, _size, returned
        ):
            ctypes.cast(returned, ctypes.POINTER(ctypes.c_uint)).contents.value = 1
            if command == NET_DVR_GET_FIRMWARE_VERSION:
                target = ctypes.cast(output, ctypes.POINTER(NET_DVR_FIRMWARE_VERSION_INFO)).contents
                target.szFirmwareVersion = b"V3.6.0 build 250522"
                return True
            if command == NET_DVR_GET_DEVICECFG_V50:
                target = ctypes.cast(output, ctypes.POINTER(NET_DVR_DEVICECFG_V50)).contents
                encoded = b"DS-KV8113-WME1"
                target.byDevTypeName[: len(encoded)] = encoded
                return True
            return False

    assert _get_device_info(FakeSDK(), 42) == {
        "manufacturer": "Hikvision",
        "model": "DS-KV8113-WME1",
        "firmware_version": "V3.6.0 build 250522",
    }


def test_worker_formats_fallback_device_config_firmware_values():
    version = (3 << 24) | (6 << 16) | 1
    build_date = (2025 << 16) | (5 << 8) | 22

    assert _format_firmware_version(version, build_date) == "V3.6.1 build 20250522"
    short_year = (23 << 16) | (12 << 8) | 13
    assert _format_firmware_version((2 << 24) | (2 << 16) | 65, short_year) == (
        "V2.2.65 build 231213"
    )
    assert _format_firmware_version(0, build_date) is None


@pytest.mark.asyncio
async def test_worker_crash_is_isolated_and_becomes_failed_health(tmp_path):
    async def preserve(_delivery: RawPluginDelivery) -> None:
        pass

    command = [
        sys.executable,
        "-c",
        _worker_source({"type": "ready"}),
    ]
    worker = SDKDeviceWorker(tmp_path, _config(), preserve, command=command)

    assert await worker.start()
    for _attempt in range(100):
        if worker.status().state == PluginInstanceState.FAILED:
            break
        await asyncio.sleep(0.001)

    assert worker.status().state == PluginInstanceState.FAILED
    assert "exited unexpectedly" in worker.status().error
    await worker.stop()


@pytest.mark.asyncio
async def test_worker_login_error_is_compact_and_does_not_expose_credentials(tmp_path):
    async def preserve(_delivery: RawPluginDelivery) -> None:
        pass

    command = [
        sys.executable,
        "-c",
        _worker_source({"type": "error", "stage": "login", "code": 1}),
    ]
    worker = SDKDeviceWorker(tmp_path, _config(), preserve, command=command)

    assert not await worker.start()

    status = worker.status()
    assert status.state == PluginInstanceState.FAILED
    assert status.error == "HCNetSDK login failed (error 1)."
    assert "sdk-user" not in repr(status)
    assert "not-exposed" not in repr(status)
    await worker.stop()


@pytest.mark.asyncio
async def test_raw_plugin_delivery_store_seals_bytes_and_records_receipt(tmp_path):
    config = EpisodeConfig(data_dir=str(tmp_path / "data"))
    repository = Repository(config)
    await repository.initialize()
    bus = EventBus()
    engine = EpisodeEngine(repository, bus, timeout=30)
    router = IngressRouter()
    store = RawPluginDeliveryStore(IngestionService(config.data_dir, repository, engine, router))
    received_at = datetime.now(tz=timezone.utc)

    await store(
        RawPluginDelivery(
            plugin_id="hikvision-sdk",
            device_id="front-doorbell",
            area_id="front-door",
            received_at=received_at,
            payload=b"immutable callback bytes",
            metadata={"command": 0x1133},
        )
    )

    receipts = await repository.list_ingestion_receipts(limit=10)
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.source == "plugin:hikvision-sdk"
    assert receipt.device_id == "front-doorbell"
    assert receipt.event_id is None
    assert receipt.episode_id is None
    artifact = await repository.get_raw_artifact(receipt.artifact_id)
    assert artifact is not None
    assert artifact.sealed
    assert Path(artifact.file_path).read_bytes() == b"immutable callback bytes"
    assert artifact.metadata == {}
    assert receipt.metadata["command"] == 0x1133

    await repository.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload_factory", "expected_event_type", "expected_metadata"),
    (
        (_ring_payload, "doorbell", {"phase": "ringing"}),
        (_tamper_payload, "tamper_detection", {"sdk_alarm_name": "anti_tamper"}),
    ),
    ids=("doorbell-ring", "anti-tamper"),
)
async def test_interpreted_alarm_delivery_uses_canonical_episode_pipeline(
    tmp_path,
    payload_factory,
    expected_event_type,
    expected_metadata,
):
    config = EpisodeConfig(data_dir=str(tmp_path / "data"), episode_timeout=30)
    repository = Repository(config)
    await repository.initialize()
    await repository.upsert_area(Area(id="front-door", name="Front Door"))
    await repository.upsert_device(
        Device(
            id="front-doorbell",
            name="Front Doorbell",
            device_type="doorbell",
            area_id="front-door",
            capabilities=["hikvision_sdk"],
        )
    )
    bus = EventBus()
    engine = EpisodeEngine(repository, bus, timeout=30)
    await engine.start()
    router = IngressRouter()
    router.register(
        IngressHandlerRegistration(
            id="hikvision-sdk-events",
            matcher=HikvisionSDKPlugin._matches_ingress,
            handler=HikvisionSDKPlugin._interpret_ingress,
        )
    )
    store = RawPluginDeliveryStore(IngestionService(config.data_dir, repository, engine, router))
    received_at = datetime.now(tz=timezone.utc)
    raw = payload_factory()

    await store(
        RawPluginDelivery(
            plugin_id="hikvision-sdk",
            device_id="front-doorbell",
            area_id="front-door",
            received_at=received_at,
            payload=raw,
            metadata={"command": 0x1133},
        )
    )

    episodes = await repository.list_episodes()
    events = await repository.list_events(device_id="front-doorbell")
    receipts = await repository.list_ingestion_receipts(limit=10)
    assert len(episodes) == 1
    assert len(events) == 1
    assert len(receipts) == 1
    assert events[0].episode_id == episodes[0].id
    assert events[0].event_type == expected_event_type
    assert events[0].event_state == "active"
    for key, value in expected_metadata.items():
        assert events[0].metadata[key] == value
    assert receipts[0].event_id == events[0].id
    assert receipts[0].episode_id == episodes[0].id
    artifact = await repository.get_raw_artifact(receipts[0].artifact_id)
    assert artifact is not None
    assert artifact.sealed
    assert f"episodes/{episodes[0].id}/events" in artifact.file_path
    assert Path(artifact.file_path).read_bytes() == raw
    assert events[0].raw_payload_path == artifact.file_path

    await engine.stop()
    await repository.close()


def test_worker_ctypes_layouts_match_hcnetsdk_6_1_9_48_header():
    structures = (
        NET_DVR_ALARMER,
        NET_DVR_DEVICEINFO_V30,
        NET_DVR_DEVICEINFO_V40,
        NET_DVR_FIRMWARE_VERSION_INFO,
        NET_DVR_DEVICECFG_V50,
        NET_DVR_USER_LOGIN_INFO,
        NET_DVR_SETUPALARM_PARAM_V50,
        NET_DVR_JPEGPARA,
    )

    assert [ctypes.sizeof(structure) for structure in structures] == [
        372,
        80,
        344,
        260,
        500,
        416,
        148,
        4,
    ]


def test_worker_validates_sdk_jpeg_capture_abi_and_markers():
    payload = b"\xff\xd8valid-jpeg\xff\xd9"

    class FakeSDK:
        @staticmethod
        def NET_DVR_CaptureJPEGPicture_NEW(  # noqa: N802 - mirrors the vendor function
            _user_id,
            channel,
            parameters,
            output,
            output_size,
            returned,
        ):
            assert channel == 1
            jpeg_parameters = ctypes.cast(parameters, ctypes.POINTER(NET_DVR_JPEGPARA)).contents
            assert jpeg_parameters.wPicSize == 0xFF
            assert jpeg_parameters.wPicQuality == 0
            assert output_size == MAX_SNAPSHOT_BYTES
            ctypes.memmove(output, payload, len(payload))
            ctypes.cast(returned, ctypes.POINTER(ctypes.c_uint)).contents.value = len(payload)
            return True

    assert _capture_jpeg(FakeSDK(), 42) == payload

    class InvalidSDK(FakeSDK):
        @staticmethod
        def NET_DVR_CaptureJPEGPicture_NEW(*_args):  # noqa: N802
            return True

    with pytest.raises(SnapshotCaptureError, match="invalid JPEG size"):
        _capture_jpeg(InvalidSDK(), 42)

    class FailingSDK:
        @staticmethod
        def NET_DVR_CaptureJPEGPicture_NEW(*_args):  # noqa: N802
            return False

        @staticmethod
        def NET_DVR_GetLastError():  # noqa: N802
            return 17

    with pytest.raises(SnapshotCaptureError) as error:
        _capture_jpeg(FailingSDK(), 42)
    assert error.value.code == 17


@pytest.mark.asyncio
async def test_worker_startup_timeout_terminates_hung_process(tmp_path):
    async def preserve(_delivery: RawPluginDelivery) -> None:
        pass

    command = [
        sys.executable,
        "-c",
        "import json, sys, time; json.loads(sys.stdin.readline()); time.sleep(60)",
    ]
    worker = SDKDeviceWorker(
        tmp_path,
        _config(),
        preserve,
        command=command,
        startup_timeout=0.05,
    )

    assert not await worker.start()
