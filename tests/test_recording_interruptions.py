from __future__ import annotations

import asyncio
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from episode.config import EpisodeConfig
from episode.domain.models import (
    Area,
    CapabilityConfig,
    Device,
    Episode,
    EpisodeState,
    Event,
    EventState,
    Evidence,
)
from episode.engine.bus import EventBus, Message
from episode.engine.engine import CanonicalEventResult, EpisodeEngine
from episode.recording.engine import RecordingEngine
from episode.storage.repository import Repository


@pytest.mark.asyncio
async def test_resume_active_episodes_requests_complete_event_history():
    episode = Episode(
        id="active-episode",
        primary_area_id="test-area",
        state=EpisodeState.ACTIVE,
        minimum_end_at=datetime.now(tz=timezone.utc) + timedelta(minutes=1),
    )

    class RepositoryStub:
        def __init__(self):
            self.event_limits = []

        async def list_episodes(self, *, state, limit):
            return [episode] if state == EpisodeState.ACTIVE else []

        async def list_events(self, *, episode_id, limit):
            assert episode_id == episode.id
            self.event_limits.append(limit)
            return []

        async def get_quiescent_grace_seconds(self):
            return 5

    repository = RepositoryStub()
    recorder = RecordingEngine(repository, EventBus(), "/tmp/episode-test")

    await recorder.resume_active_episodes()

    assert repository.event_limits == [10000]


async def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not met before timeout")
        await asyncio.sleep(0.005)


def _recording_event(episode_id: str, device_id: str) -> Event:
    return Event(
        device_id=device_id,
        area_id="test-area",
        event_type="motion_detection",
        event_state=EventState.ACTIVE,
        source="test",
        episode_id=episode_id,
        eligible_recording_device_ids=[device_id],
    )


@pytest.mark.asyncio
async def test_repeated_ffmpeg_failures_reconnect_into_one_episode_recording(
    tmp_path,
    monkeypatch,
):
    config = EpisodeConfig(data_dir=str(tmp_path))
    repository = Repository(config)
    await repository.initialize()
    await repository.upsert_area(Area(id="test-area", name="Test area"))
    device = Device(
        id="camera-reconnect",
        name="Reconnect camera",
        device_type="camera",
        area_id="test-area",
        ip_address="192.0.2.10",
        configs={
            "video": CapabilityConfig(
                protocol="rtsp",
                port=554,
                path="/stream",
                settings={"recording_mode": "on_event"},
            )
        },
    )
    await repository.upsert_device(device)
    episode = Episode(
        id="reconnect-episode",
        primary_area_id="test-area",
        state=EpisodeState.ACTIVE,
    )
    await repository.create_episode(episode)

    bus = EventBus()
    recorder = RecordingEngine(repository, bus, config.data_dir)
    recorder._retry_initial_seconds = 0.01
    recorder._retry_max_seconds = 0.02
    attempts: list[tuple[str, ...]] = []
    recovery_started = asyncio.Event()
    recovery_stopped = asyncio.Event()

    class FailedProcess:
        returncode = 1

        def terminate(self):
            return None

        def kill(self):
            return None

        async def wait(self):
            return self.returncode

    class RecoveringProcess:
        def __init__(self):
            self.returncode = None

        def terminate(self):
            self.returncode = -15
            recovery_stopped.set()

        def kill(self):
            self.returncode = -9
            recovery_stopped.set()

        async def wait(self):
            await recovery_stopped.wait()
            return self.returncode

    async def start_ffmpeg(*args, **kwargs):
        attempts.append(tuple(str(arg) for arg in args))
        if len(attempts) <= 5:
            return FailedProcess()

        root = Path(kwargs["cwd"])
        (root / "init.mp4").write_bytes(b"init")
        (root / "segments" / "segment-000000.m4s").write_bytes(b"video-fragment")
        (root / "index.m3u8").write_text(
            "#EXTM3U\n"
            '#EXT-X-MAP:URI="init.mp4"\n'
            "#EXT-X-DISCONTINUITY\n"
            "#EXTINF:4.0,\nsegments/segment-000000.m4s\n",
            encoding="utf-8",
        )
        recovery_started.set()
        return RecoveringProcess()

    async def persist_evidence(message):
        await repository.create_evidence(Evidence(**message.data["evidence"]))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start_ffmpeg)
    bus.subscribe("evidence.received", persist_evidence)
    try:
        await recorder.start()
        await bus.publish(
            Message(
                type="event.canonicalized",
                data={
                    "result": CanonicalEventResult(_recording_event(episode.id, device.id), True)
                },
            )
        )
        key = (episode.id, device.id)
        recording = recorder._recordings[key]
        evidence_id = recording.evidence_id

        await _wait_until(lambda: recorder.status()["reconnects"] >= 4)
        assert len(attempts) == 4
        assert recorder.status()["recordings"][0]["state"] == "reconnecting"
        assert await repository.list_evidence(episode_id=episode.id, limit=10) == []

        # A later Event during recovery must attach to the existing session, not
        # create a second Evidence identity or HLS workspace for this camera.
        await bus.publish(
            Message(
                type="event.canonicalized",
                data={
                    "result": CanonicalEventResult(_recording_event(episode.id, device.id), True)
                },
            )
        )
        assert recorder._recordings[key] is recording
        assert len(list((tmp_path / "episodes" / episode.id / "recordings").iterdir())) == 1

        await asyncio.wait_for(recovery_started.wait(), timeout=2)
        recovery_flags = attempts[-1][attempts[-1].index("-hls_flags") + 1]
        assert "append_list" in recovery_flags
        assert "discont_start" in recovery_flags
        assert "#EXT-X-DISCONTINUITY" in recording.bundle.playlist_path.read_text()

        await recorder.finalize_episode(episode.id)
        evidence = await repository.list_evidence(episode_id=episode.id, limit=10)
        assert len(evidence) == 1
        assert evidence[0].id == evidence_id
        assert evidence[0].evidence_type == "recording"
        assert evidence[0].file_path == str(recording.bundle.playlist_path)
    finally:
        await recorder.stop()
        await repository.close()


@pytest.mark.parametrize("action", ["finalize", "shutdown"])
@pytest.mark.asyncio
async def test_retry_backoff_is_interruptible_when_camera_is_offline(tmp_path, monkeypatch, action):
    config = EpisodeConfig(data_dir=str(tmp_path))
    repository = Repository(config)
    await repository.initialize()
    await repository.upsert_area(Area(id="test-area", name="Test area"))
    device = Device(
        id="camera-offline",
        name="Offline camera",
        device_type="camera",
        area_id="test-area",
        ip_address="192.0.2.11",
        configs={
            "video": CapabilityConfig(
                protocol="rtsp",
                port=554,
                path="/stream",
                settings={"recording_mode": "on_event"},
            )
        },
    )
    await repository.upsert_device(device)
    episode = Episode(
        id="offline-episode",
        primary_area_id="test-area",
        state=EpisodeState.ACTIVE,
    )
    await repository.create_episode(episode)

    bus = EventBus()
    recorder = RecordingEngine(repository, bus, config.data_dir)
    recorder._retry_initial_seconds = 10
    recorder._retry_max_seconds = 10
    attempts = 0
    publication_attempts = 0

    class FailedProcess:
        returncode = 1

        def terminate(self):
            return None

        def kill(self):
            return None

        async def wait(self):
            return self.returncode

    async def start_ffmpeg(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        return FailedProcess()

    async def persist_evidence(message):
        nonlocal publication_attempts
        publication_attempts += 1
        if action == "finalize" and publication_attempts == 1:
            raise RuntimeError("temporary Evidence persistence failure")
        await repository.create_evidence(Evidence(**message.data["evidence"]))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start_ffmpeg)
    bus.subscribe("evidence.received", persist_evidence)
    try:
        await recorder.start()
        await bus.publish(
            Message(
                type="event.canonicalized",
                data={
                    "result": CanonicalEventResult(_recording_event(episode.id, device.id), True)
                },
            )
        )
        recording = recorder._recordings[(episode.id, device.id)]
        await _wait_until(lambda: recorder.status()["reconnects"] == 1)
        assert attempts == 1
        assert recorder.status()["state"] == "degraded"

        if action == "finalize":
            await repository.update_episode_state(episode.id, EpisodeState.FINALIZING)
            await asyncio.wait_for(recorder.finalize_episode(episode.id), timeout=0.5)
            evidence = await repository.list_evidence(episode_id=episode.id, limit=10)
            assert len(evidence) == 1
            assert evidence[0].id == recording.evidence_id
            assert evidence[0].evidence_type == "incomplete_recording"
            assert not recording.bundle.capture_state_path.exists()
            assert publication_attempts == 2
        else:
            await asyncio.wait_for(recorder.stop(), timeout=0.5)
            evidence = await repository.list_evidence(episode_id=episode.id, limit=10)
            assert evidence == []
            assert recording.bundle.capture_state_path.exists()
            assert publication_attempts == 0

        assert attempts == 1
    finally:
        await recorder.stop()
        await repository.close()


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="FFmpeg process test requires ffmpeg and ffprobe",
)
@pytest.mark.parametrize("camera_count", [1, 2])
@pytest.mark.asyncio
async def test_abrupt_ffmpeg_termination_is_reconciled_as_visible_evidence(
    tmp_path,
    camera_count,
):
    config = EpisodeConfig(data_dir=str(tmp_path))
    repository = Repository(config)
    await repository.initialize()
    await repository.upsert_area(Area(id="test-area", name="Test area"))
    devices = []
    for index in range(camera_count):
        device = Device(
            id=f"camera-{index}",
            name=f"Camera {index}",
            device_type="camera",
            area_id="test-area",
            configs={
                "video": CapabilityConfig(
                    protocol="rtsp",
                    port=554,
                    path="/stream",
                    settings={"recording_mode": "on_event"},
                )
            },
        )
        devices.append(device)
        await repository.upsert_device(device)
    episode = Episode(
        id="interrupted-episode",
        primary_area_id="test-area",
        state=EpisodeState.FINALIZING,
    )
    await repository.create_episode(episode)
    recordings_dir = tmp_path / "episodes" / episode.id / "recordings"
    recordings_dir.mkdir(parents=True, exist_ok=True)

    processes = []
    for index, device in enumerate(devices):
        working_pattern = recordings_dir / (
            f"rec_{device.id}_20260824_12000{index}_000000_{index + 1:012x}_%06d.mp4.part"
        )
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-re",
            "-i",
            "testsrc=size=64x64:rate=10",
            "-map",
            "0",
            "-c:v",
            "mpeg4",
            "-f",
            "segment",
            "-segment_time",
            "600",
            "-reset_timestamps",
            "1",
            "-segment_format",
            "mp4",
            str(working_pattern),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        processes.append(process)

    try:
        deadline = asyncio.get_running_loop().time() + 8
        while True:
            partials = list(recordings_dir.glob("*.mp4.part"))
            if len(partials) == camera_count and all(path.stat().st_size > 0 for path in partials):
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("FFmpeg did not produce working segments before the deadline")
            await asyncio.sleep(0.05)

        for process in processes:
            process.kill()
        await asyncio.gather(*(process.wait() for process in processes))

        bus = EventBus()
        engine = EpisodeEngine(repository, bus, timeout=30)
        recorder = RecordingEngine(repository, bus, config.data_dir)
        recorder.set_evidence_sink(engine.ingest_recording_evidence)
        await engine.start(defer_finalization=True)
        await recorder.start()
        await recorder.recover_interrupted_recordings()

        evidence = await repository.list_evidence(episode_id=episode.id, limit=10)
        assert len(evidence) == camera_count
        assert {item.evidence_type for item in evidence}.issubset(
            {"recording", "incomplete_recording"}
        )
        assert all(os.path.isfile(item.file_path) for item in evidence)
        assert not list(recordings_dir.glob("*.mp4.part"))

        await recorder.stop()
        await engine.stop()
    finally:
        for process in processes:
            if process.returncode is None:
                process.kill()
                await process.wait()
        await repository.close()
