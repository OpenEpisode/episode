"""G8: a camera that stops answering must end its ffmpeg, and say why, safely.

Two operator-visible gaps are closed here. The recorder used to hand ffmpeg a camera URL
with no socket timeout, so a device that accepted the TCP connection and then answered
nothing held an ffmpeg child open indefinitely; and ffmpeg's stderr, the only explanation
of a failed capture, was discarded. Both are measured against a real ``ffmpeg`` and a
socket server that behaves like a half-dead camera.

The security half matters as much as the bound: ffmpeg prints the input URL verbatim when
it cannot open it, and the recorder's URL carries the camera username and password, so the
kept tail must be redacted before it reaches a log line or a status field.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from episode.config import EpisodeConfig
from episode.domain.models import (
    Area,
    CapabilityConfig,
    Device,
    Episode,
    EpisodeState,
    Evidence,
)
from episode.engine.bus import EventBus
from episode.recording.engine import (
    FFMPEG_STDERR_TAIL_CHARS,
    RTSP_SOCKET_TIMEOUT_SECONDS,
    RecordingEngine,
    _EpisodeRecording,
)
from episode.recording.hls import HLSCaptureState, HLSRecordingBundle
from episode.storage.repository import Repository

needs_ffmpeg = pytest.mark.skipif(
    not shutil.which("ffmpeg"), reason="this module proves ffmpeg is bounded, so it needs ffmpeg"
)

FAKE_USER = "recorder-user"
FAKE_PASS = "Rec0rder-Passw0rd"
FLAGS = "independent_segments+program_date_time+temp_file"


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _video_device(device_id: str, area_id: str = "area-1") -> Device:
    return Device(
        id=device_id,
        name=device_id,
        device_type="camera",
        area_id=area_id,
        capabilities=["video"],
        ip_address="192.0.2.10",
        configs={
            "video": CapabilityConfig(
                protocol="rtsp",
                port=554,
                path="/stream",
                settings={"recording_mode": "on_episode"},
            )
        },
    )


def _bundle_recording(tmp_path: Path) -> _EpisodeRecording:
    """A recording with no task attached, for testing the pure command/redaction helpers."""
    return _EpisodeRecording(
        episode_id="ep",
        device_id="cam",
        area_id="area-1",
        session_id="session",
        bundle=HLSRecordingBundle.create(
            tmp_path / "bundle",
            HLSCaptureState(
                evidence_id="ev",
                episode_id="ep",
                device_id="cam",
                area_id="area-1",
                session_id="session",
                started_at=_now(),
            ),
        ),
        start_time=_now(),
    )


class DeadCamera:
    """A TCP listener that accepts and then never replies, like a half-dead camera."""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self.host = host
        self.port = 0
        self.connections = 0
        self._handlers: set[asyncio.Task] = set()
        self._server: asyncio.Server | None = None

    async def __aenter__(self) -> DeadCamera:
        def on_connect(_reader, writer):
            self.connections += 1

            async def hold():
                try:
                    await asyncio.sleep(600)
                except asyncio.CancelledError:
                    raise
                finally:
                    writer.close()

            task = asyncio.create_task(hold())
            self._handlers.add(task)
            task.add_done_callback(self._handlers.discard)

        self._server = await asyncio.start_server(on_connect, self.host, 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_exc) -> None:
        assert self._server is not None
        self._server.close()
        # wait_closed() would block on the held connections, so the handlers are cancelled
        # first and it is skipped: this is the teardown the socket tests already learned.
        for task in list(self._handlers):
            task.cancel()
        if self._handlers:
            await asyncio.gather(*self._handlers, return_exceptions=True)

    def url(self) -> str:
        return f"rtsp://{FAKE_USER}:{FAKE_PASS}@{self.host}:{self.port}/"


@pytest.fixture
def config(tmp_path):
    return EpisodeConfig(data_dir=str(tmp_path), db_path=str(tmp_path / "test.db"))


@pytest.fixture
def recorder_repo(config):
    # Left un-initialized and un-closed on purpose: the async tests initialize and close it
    # themselves, exactly as the rest of the recording suite does.
    return Repository(config)


@pytest.fixture
def bus():
    return EventBus()


def _wire_evidence(store: Repository, bus: EventBus) -> None:
    """Publishing an incomplete bundle needs a sink, as it does in every recording test."""

    async def persist_evidence(message):
        await store.create_evidence(Evidence(**message.data["evidence"]))

    bus.subscribe("evidence.received", persist_evidence)


async def _episode(store: Repository, episode_id: str) -> None:
    await store.initialize()
    await store.upsert_area(Area(id="area-1", name="Area 1"))
    await store.upsert_device(_video_device("camera-x"))
    await store.create_episode(
        Episode(id=episode_id, primary_area_id="area-1", state=EpisodeState.ACTIVE)
    )


def test_recorder_bounds_the_camera_socket_by_default(recorder_repo, bus, config):
    """No operator knob needed: the bound exists and stays under the no-progress watchdog."""
    recorder = RecordingEngine(recorder_repo, bus, config.data_dir)
    assert recorder._socket_timeout_seconds == RTSP_SOCKET_TIMEOUT_SECONDS
    assert recorder._socket_timeout_seconds < recorder._stall_seconds

    tiny = RecordingEngine(recorder_repo, bus, config.data_dir, fragment_seconds=1)
    # A shorter watchdog must still win, or a stalled stream is reported for a socket that
    # ffmpeg had already given up on.
    assert tiny._socket_timeout_seconds <= tiny._stall_seconds - 1


def test_url_command_sets_the_rtsp_socket_timeout(recorder_repo, bus, config, tmp_path):
    """The flag must be the one the RTSP demuxer reads, in microseconds, before ``-i``.

    ``-rw_timeout`` is what a general answer suggests and what the RTSP demuxer ignores; on
    ffmpeg 8.1.2 a silent camera was still running after 20 s with ``-rw_timeout`` set, and
    exited in 3.2 s with ``-timeout``. A test looking only for "some timeout" would pass on
    a command that bounds nothing, so the flag name is asserted too.
    """
    recorder = RecordingEngine(recorder_repo, bus, config.data_dir)
    command = recorder._rtsp_command(_bundle_recording(tmp_path), "rtsp://camera/stream", FLAGS)
    assert "-rw_timeout" not in command
    index = command.index("-timeout")
    assert command[index + 1] == str(int(RTSP_SOCKET_TIMEOUT_SECONDS * 1_000_000))
    assert command.index("-i") > index
    # The piped input has no socket to time out, and must not be given one.
    piped = recorder._piped_command(_bundle_recording(tmp_path), FLAGS)
    assert "-timeout" not in piped


def test_credential_bearing_urls_are_redacted():
    text = (
        f"Input #0, rtsp, from 'rtsp://{FAKE_USER}:{FAKE_PASS}@192.0.2.7:554/live':\n"
        "Connection to tcp://192.0.2.7:554 timed out\n"
        f"Error opening input file rtsp://{FAKE_USER}:{FAKE_PASS}@192.0.2.7:554/live.\n"
    )
    redacted = RecordingEngine._redact_url_credentials(text)
    assert FAKE_PASS not in redacted
    assert FAKE_USER not in redacted
    # Host and port survive: they are what tells the operator which camera failed.
    assert "192.0.2.7:554" in redacted
    assert "[redacted]@192.0.2.7:554" in redacted


def test_stderr_note_is_bounded_and_single_line(tmp_path):
    recording = _bundle_recording(tmp_path)
    recording.ffmpeg_stderr = "alpha\nbeta\n" + "x" * 4000
    note = RecordingEngine._stderr_note(recording)
    assert "\n" not in note
    assert note.startswith(" ffmpeg: ")
    assert len(note) <= 180 + len(" ffmpeg: ")


def test_stderr_note_is_empty_when_ffmpeg_said_nothing(tmp_path):
    """A child with nothing to say must not give the reason a dangling clause."""
    recording = _bundle_recording(tmp_path)
    recording.ffmpeg_stderr = "   "
    assert RecordingEngine._stderr_note(recording) == ""


def test_stderr_tail_is_bounded_while_reading(recorder_repo, bus, config, tmp_path):
    """The reader cannot grow without limit on a chatty ffmpeg, only the tail survives."""
    recording = _bundle_recording(tmp_path)

    class NoisyStream:
        def __init__(self) -> None:
            self.lines = [b"x" * 500 + b"\n"] * 200

        async def readline(self):
            return self.lines.pop(0) if self.lines else b""

    class NoisyProcess:
        stderr = NoisyStream()

    asyncio.run(RecordingEngine._drain_stderr(recording, NoisyProcess()))
    assert len(recording.ffmpeg_stderr) <= FFMPEG_STDERR_TAIL_CHARS


@needs_ffmpeg
@pytest.mark.asyncio
async def test_a_silent_camera_ends_its_ffmpeg_and_the_attempt_reconnects(
    recorder_repo, bus, config
):
    """The bound is real: with a silent camera the attempt ends in socket-timeout time."""
    await _episode(recorder_repo, "episode-dead")
    recorder = RecordingEngine(recorder_repo, bus, config.data_dir)
    recorder._socket_timeout_seconds = 2.0
    recorder._stall_seconds = 5.0

    async with DeadCamera() as camera:
        started = time.monotonic()
        await recorder.start()
        await recorder._start_recording("episode-dead", _video_device("camera-x"), camera.url())
        recording = recorder._recordings[("episode-dead", "camera-x")]
        while not recorder.status()["reconnects"]:
            if time.monotonic() - started > 20:
                pytest.fail("recorder never gave up on a camera that answers nothing")
            await asyncio.sleep(0.05)
        elapsed = time.monotonic() - started

    # ~2 s of socket timeout, not the unbounded hang this measurement replaced.
    assert elapsed < 12, f"socket timeout did not bound ffmpeg (took {elapsed:.1f}s)"
    assert camera.connections >= 1
    assert recording.last_exit_code not in (None, 0)
    assert "ffmpeg:" in (recording.last_error or "")
    assert FAKE_PASS not in (recording.last_error or "")
    assert FAKE_USER not in (recording.last_error or "")
    assert "[redacted]@" in (recording.last_error or "")
    assert FAKE_PASS not in str(recorder.status())

    await recorder._stop_recording(recording, reason="test_done")
    await recorder.stop()
    await recorder_repo.close()


@needs_ffmpeg
@pytest.mark.asyncio
async def test_credentials_never_reach_a_log_line(recorder_repo, bus, config, caplog):
    """Redaction happens where the tail is kept, so no log line can carry the password."""
    await _episode(recorder_repo, "episode-log")
    recorder = RecordingEngine(recorder_repo, bus, config.data_dir)
    recorder._socket_timeout_seconds = 2.0
    recorder._stall_seconds = 5.0

    async with DeadCamera() as camera:
        with caplog.at_level(logging.DEBUG, logger="episode.recording.engine"):
            await recorder.start()
            await recorder._start_recording("episode-log", _video_device("camera-x"), camera.url())
            while not recorder.status()["reconnects"]:
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.1)

    joined = "\n".join(record.getMessage() for record in caplog.records)
    assert FAKE_PASS not in joined
    assert FAKE_USER not in joined
    assert "ffmpeg:" in joined, "the failed attempt should have logged ffmpeg's own reason"

    await recorder.stop()
    await recorder_repo.close()


@pytest.mark.asyncio
async def test_a_child_without_stderr_keeps_a_plain_reconnect_reason(
    recorder_repo, bus, config, monkeypatch
):
    """ffmpeg prints a banner even when healthy, so a redialled stream keeps no stderr noise."""

    class SilentFailedProcess:
        returncode = 1
        stderr = None

        async def wait(self):
            return self.returncode

    async def failed_ffmpeg(*args, **kwargs):
        return SilentFailedProcess()

    _wire_evidence(recorder_repo, bus)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", failed_ffmpeg)
    await _episode(recorder_repo, "episode-quiet")
    recorder = RecordingEngine(recorder_repo, bus, config.data_dir)
    await recorder.start()
    await recorder._start_recording("episode-quiet", _video_device("camera-x"), "rtsp://cam/stream")
    recording = recorder._recordings[("episode-quiet", "camera-x")]
    await asyncio.wait_for(recording.task, timeout=30)

    assert recording.last_error == "FFmpeg exited with code 1; retry limit exceeded"
    assert "ffmpeg:" not in str(recorder.status())
    await recorder.stop()
    await recorder_repo.close()
