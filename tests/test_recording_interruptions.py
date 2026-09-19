from __future__ import annotations

import asyncio
import os
import shutil
from datetime import datetime, timedelta, timezone

import pytest

from episode.config import EpisodeConfig
from episode.domain.models import Area, CapabilityConfig, Device, Episode, EpisodeState
from episode.engine.bus import EventBus
from episode.engine.engine import EpisodeEngine
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
