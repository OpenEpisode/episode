from __future__ import annotations

import asyncio
import json

import pytest

from episode.domain.models import CapabilityConfig, Device
from episode.inventory.video_validation import validate_manual_video


def _device(
    *,
    protocol: str = "rtsp",
    path: str = "/stream",
    settings: dict | None = None,
) -> Device:
    return Device(
        id="camera-test",
        name="Test camera",
        device_type="camera",
        area_id="entrance",
        ip_address="192.0.2.10",
        username="user@example.com",
        password="p@ss:word",
        configs={
            "video": CapabilityConfig(
                protocol=protocol,
                port=8554,
                path=path,
                settings=settings or {},
            )
        },
    )


class _FakeStdout:
    def __init__(self, output: bytes = b"", wait_forever: bool = False):
        self.output = output
        self.wait_forever = wait_forever
        self.read_once = False

    async def read(self, _size: int) -> bytes:
        if self.wait_forever:
            await asyncio.Event().wait()
        if self.read_once:
            return b""
        self.read_once = True
        return self.output


class _FakeProcess:
    def __init__(self, output: bytes = b"", wait_forever: bool = False):
        self.stdout = _FakeStdout(output, wait_forever)
        self.returncode: int | None = None
        self.killed = False
        self.reaped = asyncio.Event()
        self._wait_forever = wait_forever

    async def wait(self) -> int:
        if self._wait_forever:
            await self.reaped.wait()
        return self.returncode or 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self.reaped.set()


@pytest.mark.asyncio
async def test_manual_video_validation_reports_codec_without_url(monkeypatch):
    process = _FakeProcess(json.dumps({"streams": [{"codec_name": "h264"}]}).encode())
    calls = []

    async def start(*args, **kwargs):
        calls.append((args, kwargs))
        process.returncode = 0
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)

    result = await validate_manual_video(_device())

    assert result == {
        "status": "supported",
        "summary": "The manual video stream responded",
        "details": {"codec": "h264"},
    }
    assert calls[0][0][-1] == "rtsp://user%40example.com:p%40ss%3Aword@192.0.2.10:8554/stream"
    assert "192.0.2.10" not in repr(result)
    assert "p@ss" not in repr(result)
    assert calls[0][1]["stderr"] is asyncio.subprocess.DEVNULL


@pytest.mark.asyncio
async def test_manual_video_validation_rejects_non_manual_or_non_rtsp(monkeypatch):
    async def fail_start(*_args, **_kwargs):
        raise AssertionError("ffprobe must not start")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_start)

    discovered = await validate_manual_video(_device(settings={"origin": "onvif"}))
    unsupported = await validate_manual_video(_device(protocol="http"))

    assert discovered["status"] == "unavailable"
    assert unsupported["status"] == "unavailable"


@pytest.mark.asyncio
async def test_manual_video_validation_reports_probe_failure(monkeypatch):
    process = _FakeProcess(b"not json")
    process.returncode = 1

    async def start(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)

    result = await validate_manual_video(_device())

    assert result["status"] == "unavailable"
    assert "not json" not in repr(result)
    assert "192.0.2.10" not in repr(result)


@pytest.mark.asyncio
async def test_manual_video_validation_kills_probe_on_timeout(monkeypatch):
    process = _FakeProcess(wait_forever=True)

    async def start(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    monkeypatch.setattr(
        "episode.inventory.video_validation._FFPROBE_TIMEOUT_SECONDS",
        0.01,
    )

    result = await validate_manual_video(_device())

    assert result["status"] == "unavailable"
    assert process.killed is True
    assert process.reaped.is_set()


@pytest.mark.asyncio
async def test_manual_video_validation_kills_and_reaps_on_cancellation(monkeypatch):
    process = _FakeProcess(wait_forever=True)

    async def start(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    task = asyncio.create_task(validate_manual_video(_device()))
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.killed is True
    assert process.reaped.is_set()


@pytest.mark.asyncio
async def test_manual_video_validation_bounds_concurrent_probes(monkeypatch):
    async def fail_start(*_args, **_kwargs):
        raise AssertionError("A busy validator must not spawn ffprobe")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_start)
    monkeypatch.setattr("episode.inventory.video_validation._PROBE_SLOTS", asyncio.Semaphore(0))

    result = await validate_manual_video(_device())

    assert result == {
        "status": "unavailable",
        "summary": "Video validation is busy; retry shortly",
        "details": {},
    }


def test_capability_url_encodes_credentials_for_manual_streams():
    config = CapabilityConfig(protocol="rtsps", port=8554, path="/stream")

    assert (
        config.build_url("192.0.2.10", "user@example.com", "p@ss:word")
        == "rtsps://user%40example.com:p%40ss%3Aword@192.0.2.10:8554/stream"
    )
    assert (
        config.build_url("2001:db8::10", "user", "password")
        == "rtsps://user:password@[2001:db8::10]:8554/stream"
    )
