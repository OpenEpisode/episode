from __future__ import annotations

import asyncio
import base64
import ctypes
import json
import sys

import pytest

from episode.plugins.hikvision.sdk.runtime import SDKDeviceConfig
from episode.plugins.hikvision.sdk.video_runtime import HikvisionSDKVideoWorker
from episode.plugins.hikvision.sdk.video_worker import (
    MESSAGE_PREFIX,
    NET_DVR_COMPRESSIONCFG_V30,
    stream_capabilities,
)
from episode.plugins.models import PluginInstanceState


def _config() -> SDKDeviceConfig:
    return SDKDeviceConfig(
        id="garage-camera",
        name="Garage Camera",
        area_id="garage",
        address="192.0.2.20",
        port=8000,
        username="camera-user",
        password="camera-password",
    )


class FakeMediaRegistry:
    def __init__(self):
        self.sources = {}

    def register(self, source):
        self.sources[source.video_source.id] = source

    def get(self, device_id, *, source_id=None):
        return self.sources.get(source_id)

    def unregister(self, device_id, *, source=None):
        self.sources = {
            source_id: media
            for source_id, media in self.sources.items()
            if source is not None and media.source != source
        }


def _worker_command(stream_bytes: bytes) -> list[str]:
    ready = {
        "type": "ready",
        "device_info": {
            "manufacturer": "Hikvision",
            "model": "DS-2CD2347G2",
            "firmware_version": "V5.7.1",
        },
        "streams": [
            {
                "variant": "main",
                "stream_type": 0,
                "codec": "h264",
                "width": 2560,
                "height": 1440,
                "frame_rate": 25,
            },
            {
                "variant": "sub",
                "stream_type": 1,
                "codec": "hevc",
                "width": 640,
                "height": 360,
                "frame_rate": 15,
            },
        ],
    }
    video = {
        "type": "video",
        "length": len(stream_bytes),
        "payload": base64.b64encode(stream_bytes).decode(),
    }
    started = MESSAGE_PREFIX + json.dumps(
        {"type": "preview_started", "stream_type": 0},
        separators=(",", ":"),
    )
    packet_message = MESSAGE_PREFIX + json.dumps(video, separators=(",", ":"))
    stopped = MESSAGE_PREFIX + json.dumps({"type": "preview_stopped"}, separators=(",", ":"))
    script = "\n".join(
        (
            "import json, sys",
            "sys.stdin.readline()",
            f"print({MESSAGE_PREFIX + json.dumps(ready, separators=(',', ':'))!r}, flush=True)",
            "for line in sys.stdin:",
            "    command = json.loads(line)",
            "    if command.get('type') == 'start':",
            f"        print({started!r}, flush=True)",
            f"        print({packet_message!r}, flush=True)",
            "    elif command.get('type') == 'stop':",
            f"        print({stopped!r}, flush=True)",
            "        break",
        )
    )
    return [sys.executable, "-c", script]


@pytest.mark.asyncio
async def test_camera_video_worker_registers_sources_and_feeds_recording(tmp_path):
    registry = FakeMediaRegistry()
    packet = b"\x00\x00\x00\x01\x65sample-h264-access-unit"
    worker = HikvisionSDKVideoWorker(
        tmp_path,
        _config(),
        registry,
        command=_worker_command(packet),
    )

    assert await worker.start()
    assert worker.status().state == PluginInstanceState.RUNNING
    assert worker.status().device_info.model == "DS-2CD2347G2"
    assert set(registry.sources) == {"hikvision-sdk:main", "hikvision-sdk:sub"}
    assert registry.sources["hikvision-sdk:main"].codec_hint == "h264"
    assert registry.sources["hikvision-sdk:sub"].codec_hint == "hevc"
    assert registry.sources["hikvision-sdk:main"].video_source.width == 2560
    assert registry.sources["hikvision-sdk:sub"].video_source.frame_rate == 15

    pushed = []
    packet_pushed = asyncio.Event()

    async def push(chunk):
        pushed.append(chunk)
        packet_pushed.set()

    capture = asyncio.create_task(registry.sources["hikvision-sdk:main"].video_handler(push))
    await asyncio.wait_for(packet_pushed.wait(), timeout=2)
    capture.cancel()
    await asyncio.gather(capture, return_exceptions=True)

    assert pushed == [packet]
    await worker.stop()
    assert worker.status().state == PluginInstanceState.STOPPED
    assert registry.sources == {}


@pytest.mark.asyncio
async def test_stopping_video_worker_releases_active_recorder_handler(tmp_path):
    worker = HikvisionSDKVideoWorker(
        tmp_path,
        _config(),
        FakeMediaRegistry(),
        command=_worker_command(b"\x00\x00\x00\x01\x65sample-h264-access-unit"),
    )
    assert await worker.start()

    pushed = asyncio.Event()

    async def push(_chunk):
        pushed.set()

    handler = asyncio.create_task(
        worker._handler_for(0)(push),
    )
    await asyncio.wait_for(pushed.wait(), timeout=2)
    await worker.stop()

    result = await asyncio.wait_for(asyncio.gather(handler, return_exceptions=True), timeout=2)
    assert isinstance(result[0], RuntimeError)


@pytest.mark.asyncio
async def test_crashed_video_worker_removes_unavailable_sources(tmp_path):
    registry = FakeMediaRegistry()
    worker = HikvisionSDKVideoWorker(
        tmp_path,
        _config(),
        registry,
        command=_worker_command(b"\x00\x00\x00\x01\x65sample-h264-access-unit"),
    )
    assert await worker.start()
    assert registry.sources

    worker._process.kill()
    await asyncio.wait_for(worker._wait_task, timeout=2)

    assert registry.sources == {}
    assert worker.status().state == PluginInstanceState.FAILED
    await worker.stop(preserve_failure=True)


def test_stream_capabilities_read_current_encoder_settings():
    class FakeSDK:
        @staticmethod
        def NET_DVR_GetDVRConfig(  # noqa: N802 - mirrors vendor SDK
            _user_id, _command, _channel, output, _size, returned
        ):
            cfg = ctypes.cast(output, ctypes.POINTER(NET_DVR_COMPRESSIONCFG_V30)).contents
            cfg.struNormHighRecordPara.byVideoEncType = 1
            cfg.struNormHighRecordPara.byResolution = 19
            cfg.struNormHighRecordPara.dwVideoFrameRate = 17
            cfg.struNetPara.byVideoEncType = 10
            cfg.struNetPara.byResolution = 86
            cfg.struNetPara.dwVideoFrameRate = 14
            ctypes.cast(returned, ctypes.POINTER(ctypes.c_uint)).contents.value = ctypes.sizeof(cfg)
            return True

    assert stream_capabilities(FakeSDK(), 42, 1) == [
        {
            "variant": "main",
            "stream_type": 0,
            "codec": "h264",
            "width": 1280,
            "height": 720,
            "frame_rate": 25,
        },
        {
            "variant": "sub",
            "stream_type": 1,
            "codec": "hevc",
            "width": 640,
            "height": 360,
            "frame_rate": 15,
        },
    ]


def test_stream_capabilities_omit_unknown_codec_and_keep_unknown_dimensions():
    class FakeSDK:
        @staticmethod
        def NET_DVR_GetDVRConfig(  # noqa: N802 - mirrors vendor SDK
            _user_id, _command, _channel, output, _size, _returned
        ):
            cfg = ctypes.cast(output, ctypes.POINTER(NET_DVR_COMPRESSIONCFG_V30)).contents
            cfg.struNormHighRecordPara.byVideoEncType = 7
            cfg.struNetPara.byVideoEncType = 0  # Private H.264 is not advertised yet.
            cfg.struNetPara.byResolution = 253
            cfg.struNetPara.dwVideoFrameRate = 0
            return True

    assert (
        stream_capabilities(FakeSDK(), 42, 1)
        == [
            # Neither private H.264 nor an unknown encoding is offered as a source.
        ]
    )


def test_stream_capabilities_reject_incomplete_sdk_response():
    class FakeSDK:
        @staticmethod
        def NET_DVR_GetDVRConfig(  # noqa: N802 - mirrors vendor SDK
            _user_id, _command, _channel, output, _size, returned
        ):
            cfg = ctypes.cast(output, ctypes.POINTER(NET_DVR_COMPRESSIONCFG_V30)).contents
            cfg.struNormHighRecordPara.byVideoEncType = 1
            ctypes.cast(returned, ctypes.POINTER(ctypes.c_uint)).contents.value = 1
            return True

    assert stream_capabilities(FakeSDK(), 42, 1) == []
