"""Contract spike: a plugin video handler must produce today's bundle, not a new one.

The question this module answers with real ``ffmpeg`` and no camera is whether the
recorder keeps owning HLS when the bytes arrive from a plugin: same playlist shape,
same component kinds, same Evidence, same retention. Each test drives
``RecordingEngine`` with a fake handler pushing pre-recorded Annex-B bytes, so the
contract is proven before any vendor protocol code exists to feed it.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from episode.api.thumbnails import ThumbnailCache
from episode.config import EpisodeConfig
from episode.domain.models import (
    Area,
    CapabilityConfig,
    Device,
    EventState,
    Evidence,
)
from episode.engine.bus import EventBus, Message
from episode.engine.engine import EpisodeEngine
from episode.media.registry import CameraMedia, MediaRegistry
from episode.recording.engine import RecordingEngine, _EpisodeRecording
from episode.recording.hls import HLSCaptureState, HLSRecordingBundle
from episode.retention import RetentionService
from episode.storage.repository import Repository

FIXTURE = Path(__file__).parent / "fixtures" / "media" / "hevc_4s_15fps.h265"
CHUNK = 4096
#: The fixture is 4 s at 15 fps, so this is the pace it would arrive at from a camera.
FIXTURE_SECONDS = 4.0

needs_ffmpeg = pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="ffmpeg/ffprobe required to grade a real bundle",
)


def _fixture_chunks(chunk_size: int = CHUNK) -> list[bytes]:
    data = FIXTURE.read_bytes()
    return [data[start : start + chunk_size] for start in range(0, len(data), chunk_size)]


def _fixture_access_units() -> list[bytes]:
    """The fixture cut at Annex-B start codes, i.e. one entry per framed packet.

    This is the shape a plugin handler actually holds: framed video, not arbitrary slices.
    It matters because ``-use_wallclock_as_timestamps`` makes arrival time the timestamp —
    slicing mid-frame produces ragged fragments and ffmpeg non-monotonic warnings, while
    feeding whole access units cuts fragments where a camera URL would cut them.
    """
    data = FIXTURE.read_bytes()
    marker = b"\x00\x00\x00\x01"
    offsets = []
    start = data.find(marker)
    while start >= 0:
        offsets.append(start)
        start = data.find(marker, start + 1)
    if not offsets:
        return [data]
    return [
        data[offset : offsets[index + 1] if index + 1 < len(offsets) else len(data)]
        for index, offset in enumerate(offsets)
    ]


def _handler(*chunks: bytes):
    """The smallest handler that satisfies the contract: push every byte, in order."""

    async def handler(push) -> None:
        for chunk in chunks:
            await push(chunk)

    return handler


def _live_feed(
    *chunks: bytes,
    seconds: float = FIXTURE_SECONDS,
    keep_open: bool = True,
):
    """A handler spreading its bytes over ``seconds`` of wall-clock time.

    ``-use_wallclock_as_timestamps`` makes arrival time the timestamp, so the same bytes
    dumped into the pipe in one burst really are a bundle a few tens of milliseconds long.
    Spreading them over the fixture's own 4 s is what makes a piped fixture behave like
    the live stream it was recorded from, and that is the only honest way to assert
    fragment counts or durations from it. A live source stays open after its last packet,
    as a real camera would; the recorder must explicitly stop it before its output is
    considered complete.
    """
    spacing = seconds / len(chunks) if chunks else 0.0
    feed_complete = asyncio.Event()

    async def handler(push) -> None:
        for chunk in chunks:
            await asyncio.sleep(spacing)
            await push(chunk)
        feed_complete.set()
        if keep_open:
            await asyncio.Event().wait()

    handler.feed_complete = feed_complete
    return handler


def _live_access_units():
    """A handler feeding the fixture as framed packets at its nominal 15 fps."""

    return _live_feed(*_fixture_access_units(), seconds=FIXTURE_SECONDS)


def _finite_access_units():
    """A finite source used to verify that an early clean return is incomplete."""

    return _live_feed(*_fixture_access_units(), seconds=FIXTURE_SECONDS, keep_open=False)


def _camera_device() -> Device:
    return Device(
        id="camera-native",
        name="Camera",
        device_type="camera",
        area_id="area-spike",
        capabilities=["video"],
        ip_address="192.0.2.9",
        configs={
            "video": CapabilityConfig(
                protocol="rtsp",
                port=554,
                path="/stream",
                settings={"recording_mode": "on_event"},
            )
        },
    )


class Harness:
    """Episode engine + recorder + media registry, wired as ``episode.__main__`` wires them."""

    def __init__(self, tmp_path: Path, fragment_seconds: int = 2) -> None:
        self.tmp_path = tmp_path
        self.config = EpisodeConfig(data_dir=str(tmp_path), episode_timeout=60)
        self.repo = Repository(self.config)
        self.bus = EventBus()
        self.registry = MediaRegistry()
        self.engine = EpisodeEngine(self.repo, self.bus, timeout=self.config.episode_timeout)
        self.recorder = RecordingEngine(
            self.repo, self.bus, self.config.data_dir, fragment_seconds, media=self.registry
        )
        self.recorder.set_evidence_sink(self.engine.ingest_recording_evidence)
        self.episode_id = ""

    async def start(self, handler=None, *, codec_hint: str = "hevc") -> None:
        await self.repo.initialize()
        await self.repo.upsert_area(Area(id="area-spike", name="Area"))
        await self.repo.upsert_device(_camera_device())
        if handler is not None:
            self.register(handler, codec_hint=codec_hint)
        await self.engine.start()
        await self.recorder.start()

    def register(self, handler, *, codec_hint: str = "hevc") -> None:
        self.registry.register(
            CameraMedia(
                device_id="camera-native",
                stream_uri="rtsp://192.0.2.9:554/not-used-by-a-handler",
                source="spike",
                video_handler=handler,
                codec_hint=codec_hint,
            )
        )

    async def begin(self) -> Any:
        """Start recording through the real event path, exactly as the application does."""
        await self.bus.publish(
            Message(
                type="event.received",
                data={
                    "event": {
                        "device_id": "camera-native",
                        "area_id": "area-spike",
                        "timestamp": datetime.now(tz=timezone.utc),
                        "event_type": "motion_detection",
                        "event_state": EventState.ACTIVE.value,
                        "source": "spike",
                    }
                },
            )
        )
        for _ in range(150):
            recordings = [
                recording
                for recording in self.recorder._recordings.values()
                if recording.device_id == "camera-native"
            ]
            if recordings:
                self.episode_id = recordings[0].episode_id
                return recordings[0]
            await asyncio.sleep(0.02)
        raise AssertionError("no recording started for the event's Area")

    async def await_recording(self, recording: Any, timeout: float = 90.0) -> None:
        """Wait for the recorder to finish this recording, finalization included."""
        assert recording.task is not None
        await asyncio.wait_for(recording.task, timeout=timeout)

    async def await_feed(self, recording: Any, timeout: float = 30.0) -> None:
        """Wait until a live fixture has handed every packet to the recorder."""
        feed_complete = getattr(recording.video_handler, "feed_complete", None)
        assert feed_complete is not None
        await asyncio.wait_for(feed_complete.wait(), timeout=timeout)

    async def await_handler_task(self, recording: Any, timeout: float = 30.0) -> Any:
        """Wait until the recorder has spawned ffmpeg and started feeding it.

        Registering a recording and spawning its child are separate steps, so a test that
        cares about the feeder task has to wait for it instead of assuming timing.
        """
        for _ in range(int(timeout * 20)):
            if recording.handler_task is not None:
                return recording.handler_task
            await asyncio.sleep(0.05)
        raise AssertionError("recorder never started feeding the plugin handler")

    async def evidence(self) -> Evidence:
        recordings = await self.repo.list_evidence()
        assert recordings, "no Evidence was published"
        return recordings[0]

    async def close(self) -> None:
        await self.recorder.stop()
        await self.engine.stop()
        await self.repo.close()


def _playlist_shape(playlist: Path) -> dict[str, Any]:
    text = playlist.read_text(encoding="utf-8")
    lines = text.splitlines()
    return {
        "text": text,
        "tags": sorted({line.split(":")[0] for line in lines if line.startswith("#EXT")}),
        "extinf": [
            float(line.split(":", 1)[1].split(",")[0])
            for line in lines
            if line.startswith("#EXTINF")
        ],
        "target_duration": float(
            next(line for line in lines if line.startswith("#EXT-X-TARGETDURATION")).split(":")[1]
        ),
        "map": '#EXT-X-MAP:URI="init.mp4"' in text,
        "event": "#EXT-X-PLAYLIST-TYPE:EVENT" in text,
        "program_date_time": "#EXT-X-PROGRAM-DATE-TIME" in text,
        "endlist": "#EXT-X-ENDLIST" in text,
    }


def _manifest_kinds(manifest: dict[str, Any]) -> dict[str, int]:
    kinds: dict[str, int] = {}
    for component in manifest["components"]:
        kinds[component["kind"]] = kinds.get(component["kind"], 0) + 1
    return kinds


def _probe(path: Path, entry: str) -> str:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            entry,
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return result.stdout.strip()


def _join(bundle: HLSRecordingBundle, destination: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(bundle.playlist_path),
            "-c",
            "copy",
            str(destination),
        ],
        check=True,
        timeout=60,
    )


@needs_ffmpeg
@pytest.mark.asyncio
async def test_handler_bytes_become_a_real_playable_bundle(tmp_path):
    """Piped access units become a bundle with real durations, init segment, and video."""
    harness = Harness(tmp_path)
    await harness.start(_live_access_units())
    recording = await harness.begin()
    await harness.await_feed(recording)
    await harness.recorder._stop_recording(recording, reason="test_done")

    shape = _playlist_shape(recording.bundle.playlist_path)
    assert shape["extinf"], "no segments were written from piped bytes"
    assert all(value > 0 for value in shape["extinf"]), shape
    assert shape["target_duration"] > 0
    assert shape["map"] and shape["event"] and shape["program_date_time"]
    # A fresh bundle has nothing to continue from, so it must not open on a discontinuity.
    assert "#EXT-X-DISCONTINUITY" not in shape["text"]
    assert (recording.bundle.root / "init.mp4").stat().st_size > 0

    joined = tmp_path / "joined.mp4"
    _join(recording.bundle, joined)
    assert float(_probe(joined, "format=duration")) > 3.0
    assert _probe(joined, "stream=codec_name") == "hevc"

    evidence = await harness.evidence()
    assert evidence.evidence_type == "recording"
    assert evidence.metadata["fragment_count"] == len(shape["extinf"])
    assert evidence.metadata["fragment_count"] >= 2

    await harness.close()


@needs_ffmpeg
@pytest.mark.asyncio
async def test_the_wallclock_input_flag_is_what_makes_a_piped_bundle_gradeable(tmp_path):
    """Control: the same bytes as a plain file input give a bundle with zero durations.

    This is the contract's cost, measured rather than argued. An Annex-B elementary
    stream carries no presentation timestamps, so ffmpeg's file input muxes it into one
    ``EXTINF:0.000000`` fragment that no player can lay out — the same reason a naive
    ``-i pipe:0`` fails. ``-use_wallclock_as_timestamps`` is what replaces them with
    arrival time, and this test pins both sides of that asymmetry so the flag can never
    be dropped as "just another input option".
    """
    harness = Harness(tmp_path)
    await harness.start(_live_access_units())
    recording = await harness.begin()
    await harness.await_feed(recording)
    await harness.recorder._stop_recording(recording, reason="test_done")
    piped_shape = _playlist_shape(recording.bundle.playlist_path)
    piped_manifest = recording.bundle.refresh_manifest(state="complete")

    control = tmp_path / "control"
    (control / "segments").mkdir(parents=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "hevc",
            "-i",
            str(FIXTURE),
            "-map",
            "0:v:0",
            "-c:v",
            "copy",
            "-f",
            "hls",
            "-hls_time",
            "2",
            "-hls_list_size",
            "0",
            "-hls_playlist_type",
            "event",
            "-hls_segment_type",
            "fmp4",
            "-hls_fmp4_init_filename",
            "init.mp4",
            "-hls_segment_filename",
            "segments/segment-%06d.m4s",
            "-hls_base_url",
            "segments/",
            "-start_number",
            "0",
            "-hls_flags",
            "independent_segments+program_date_time+temp_file+append_list",
            "index.m3u8",
        ],
        check=True,
        cwd=str(control),
        timeout=60,
    )

    control_shape = _playlist_shape(control / "index.m3u8")
    assert all(value == 0 for value in control_shape["extinf"]), control_shape
    assert control_shape["target_duration"] == 0

    # The piped bundle, by contrast, is the gradeable one, with the same component kinds.
    assert all(value > 0 for value in piped_shape["extinf"]), piped_shape
    assert piped_shape["target_duration"] > 0
    assert piped_shape["map"] and piped_shape["event"]
    assert _manifest_kinds(piped_manifest) == {
        "playlist": 1,
        "initialization": 1,
        "media_segment": piped_manifest["fragment_count"],
    }

    await harness.close()


@needs_ffmpeg
@pytest.mark.asyncio
async def test_handler_that_raises_mid_stream_yields_incomplete_evidence(tmp_path):
    """A plugin failure must never publish a plausible-looking recording."""
    chunks = _fixture_chunks()

    async def failing_handler(push) -> None:
        for chunk in chunks[: len(chunks) // 2]:
            await push(chunk)
        raise RuntimeError("camera session dropped")

    harness = Harness(tmp_path)
    await harness.start(failing_handler)
    recording = await harness.begin()
    await harness.await_recording(recording)

    evidence = await harness.evidence()
    assert evidence.evidence_type == "incomplete_recording"
    assert evidence.metadata["reason"] == "video_handler_failed"
    assert evidence.metadata["status"] == "incomplete"
    assert recording.state == "failed"
    assert "camera session dropped" in (recording.last_error or "")
    assert ("episode-spike", "camera-native") not in {
        (item.episode_id, item.device_id) for item in harness.recorder._recordings.values()
    }

    await harness.close()


@needs_ffmpeg
@pytest.mark.asyncio
async def test_handler_that_ends_before_episode_capture_yields_incomplete_evidence(tmp_path):
    """A clean source return is still a truncated capture while its Episode is active."""
    harness = Harness(tmp_path)
    await harness.start(_finite_access_units())
    recording = await harness.begin()
    await harness.await_recording(recording)

    evidence = await harness.evidence()
    assert evidence.evidence_type == "incomplete_recording"
    assert evidence.metadata["reason"] == "video_handler_ended"
    assert evidence.metadata["status"] == "incomplete"
    assert recording.state == "failed"
    assert "ended before Episode capture stopped" in (recording.last_error or "")

    await harness.close()


@needs_ffmpeg
@pytest.mark.asyncio
async def test_handler_that_never_returns_is_cancelled_when_recording_stops(tmp_path):
    """No plugin callback may outlive the recording it fed — that is a leaked camera session."""
    released = asyncio.Event()
    cancelled = asyncio.Event()

    async def endless_handler(push) -> None:
        try:
            await push(_fixture_chunks(1)[0])
            await released.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    harness = Harness(tmp_path)
    await harness.start(endless_handler)
    recording = await harness.begin()
    handler_task = await harness.await_handler_task(recording)
    assert not handler_task.done()

    await harness.recorder._stop_recording(recording)

    assert cancelled.is_set()
    assert handler_task.done()
    assert recording.handler_task is None

    await harness.close()


@pytest.mark.asyncio
async def test_a_handler_blocked_in_push_is_a_stalled_source_not_a_failed_plugin(tmp_path):
    """A handler that cannot hand over a chunk is a slow source, so the recorder times the
    write out instead of blaming the plugin, and it closes the pipe it owns.

    Graded without ffmpeg here on purpose: the point is the recorder's own accounting for a
    blocked write, and a real child would have to be stalled by the OS pipe buffer, which no
    assertion could rely on.
    """
    from episode.recording import engine as engine_module

    class _StuckStdin:
        def __init__(self) -> None:
            self.written = bytearray()
            self.closed = False

        def write(self, chunk: bytes) -> None:
            self.written.extend(chunk)

        async def drain(self) -> None:
            await asyncio.Event().wait()

        def close(self) -> None:
            self.closed = True

    class _StubChild:
        returncode = None

        def __init__(self, stdin: _StuckStdin) -> None:
            self.stdin = stdin

    harness = Harness(tmp_path)
    await harness.start(_handler(b"ignored"))
    stdin = _StuckStdin()
    rec = _EpisodeRecording(
        episode_id="ep-stall",
        device_id="camera-native",
        area_id="area-spike",
        session_id="0123456789ab",
        bundle=HLSRecordingBundle(
            tmp_path / "stall",
            HLSCaptureState("ev", "ep", "dev", "area", "s", datetime.now(tz=timezone.utc)),
        ),
        start_time=datetime.now(tz=timezone.utc),
        video_handler=_handler(b"\x00\x00\x00\x01frame"),
    )
    monkeypatched = engine_module.PIPE_WRITE_TIMEOUT_SECONDS
    engine_module.PIPE_WRITE_TIMEOUT_SECONDS = 0.05
    try:
        harness.recorder._start_handler(rec, _StubChild(stdin))
        await asyncio.wait_for(rec.handler_task, timeout=5.0)
    finally:
        engine_module.PIPE_WRITE_TIMEOUT_SECONDS = monkeypatched

    assert "blocked writing" in (rec.last_error or "")
    assert rec.handler_error is None, "a blocked write is not the plugin's fault"
    assert rec.handler_ended is False
    assert harness.recorder._stalled_count == 1
    assert stdin.closed, "the recorder must close the pipe it owns"
    assert bytes(stdin.written) == b"\x00\x00\x00\x01frame"

    await harness.close()


@needs_ffmpeg
@pytest.mark.asyncio
async def test_corrupt_handler_bytes_are_reported_not_silently_recorded(tmp_path):
    """Garbage instead of Annex-B produces no video, so the Evidence must say so."""
    digest = hashlib.sha256(b"not an elementary stream").digest()

    async def garbage_handler(push) -> None:
        await push(b"\x00\x00\x00\x01" + digest)

    harness = Harness(tmp_path)
    await harness.start(garbage_handler)
    recording = await harness.begin()
    await harness.await_recording(recording)

    evidence = await harness.evidence()
    assert evidence.evidence_type == "incomplete_recording"
    assert evidence.metadata["reason"] in {"video_handler_ended", "video_handler_failed"}
    assert recording.bundle.next_segment_index() == 0

    await harness.close()


@needs_ffmpeg
@pytest.mark.asyncio
async def test_a_handler_without_a_codec_hint_still_produces_video(tmp_path):
    """An empty hint lets the recorder's child guess the demuxer; a bundle is still a bundle."""
    harness = Harness(tmp_path)
    await harness.start(_live_access_units(), codec_hint="")
    recording = await harness.begin()
    await harness.await_feed(recording)
    await harness.recorder._stop_recording(recording, reason="test_done")

    shape = _playlist_shape(recording.bundle.playlist_path)
    assert shape["extinf"], shape
    assert all(value > 0 for value in shape["extinf"])
    assert (await harness.evidence()).evidence_type == "recording"

    await harness.close()


@needs_ffmpeg
@pytest.mark.asyncio
async def test_paced_handler_makes_several_fragments(tmp_path):
    """Arrival time sets duration, so a feed spread over 4 s at 1 s fragments splits."""
    harness = Harness(tmp_path, fragment_seconds=1)
    await harness.start(_live_access_units())
    recording = await harness.begin()
    await harness.await_feed(recording)
    await harness.recorder._stop_recording(recording, reason="test_done")

    shape = _playlist_shape(recording.bundle.playlist_path)
    assert len(shape["extinf"]) >= 3, shape
    assert all(value > 0 for value in shape["extinf"])

    await harness.close()


@needs_ffmpeg
@pytest.mark.asyncio
async def test_retention_removes_every_piped_bundle_component(tmp_path):
    """Handler-produced bundles are ordinary bundles: one inventory, one removal."""
    harness = Harness(tmp_path)
    await harness.start(_live_access_units())
    recording = await harness.begin()
    await harness.await_feed(recording)
    await harness.recorder._stop_recording(recording, reason="test_done")
    bundle = recording.bundle
    components = {
        path.relative_to(bundle.root).as_posix()
        for path in bundle.root.rglob("*")
        if path.is_file()
    }
    evidence = await harness.evidence()
    fragment_count = evidence.metadata["fragment_count"]
    await harness.recorder.stop()  # nothing is live any more, so retention may sweep

    assert "index.m3u8" in components
    assert "init.mp4" in components
    assert any(name.startswith("segments/") for name in components)
    assert fragment_count == sum(1 for name in components if name.startswith("segments/"))

    retention = RetentionService(
        harness.repo,
        str(tmp_path),
        ThumbnailCache(tmp_path / "cache"),
        active_paths=harness.recorder.active_file_paths,
    )
    await retention.set_policy(enabled=True, retention_days=30)
    # The bundle is minutes old, so expiry is observed from 40 days in the future.
    await retention.run_once(now=datetime.now(tz=timezone.utc) + timedelta(days=40))

    assert not bundle.root.exists()
    retained = await harness.repo.get_evidence(recording.evidence_id)
    assert retained.availability == "expired"
    assert retained.metadata["fragment_count"] == fragment_count
    assert retained.metadata["component_manifest_sha256"]
    await harness.repo.close()


def _piped_command_for(codec_hint: str, tmp_path: Path) -> list[str]:
    """Build a piped ffmpeg command for a recording with the given codec hint."""
    started_at = datetime.now(tz=timezone.utc)
    root = tmp_path / "cmd"
    bundle = HLSRecordingBundle.create(
        root,
        HLSCaptureState(
            evidence_id="ev-cmd",
            episode_id="ep-cmd",
            device_id="camera-native",
            area_id="area-spike",
            session_id="session",
            started_at=started_at,
        ),
    )
    rec = _EpisodeRecording(
        episode_id="ep-cmd",
        device_id="camera-native",
        area_id="area-spike",
        session_id="session",
        bundle=bundle,
        start_time=started_at,
        video_handler=lambda push: None,
        codec_hint=codec_hint,
    )
    recorder = RecordingEngine.__new__(RecordingEngine)
    recorder._fragment_seconds = 2
    return recorder._piped_command(rec, "independent_segments")


def test_hevc_input_is_retagged_hvc1_for_browser_playback(tmp_path):
    """A native HEVC source must be written as ``hvc1`` so WebKit can play the bundle."""
    command = _piped_command_for("hevc", tmp_path)
    assert "-tag:v" in command
    assert command[command.index("-tag:v") + 1] == "hvc1"
    assert "hev1" not in command


def test_h264_and_unknown_inputs_keep_their_natural_codec_tag(tmp_path):
    """Only a known HEVC hint gets the ``hvc1`` retag; other codecs are left alone."""
    for codec_hint in ("h264", ""):
        command = _piped_command_for(codec_hint, tmp_path)
        assert "-tag:v" not in command
