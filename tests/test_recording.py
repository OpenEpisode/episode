from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

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
from episode.recording.hls import HLSCaptureState, HLSRecordingBundle
from episode.storage.repository import Repository


def _video_device(device_id: str, area_id: str, mode: str) -> Device:
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
                settings={"recording_mode": mode},
            )
        },
    )


def _replace_recording_processes(recorder: RecordingEngine):
    started = []
    stopped = []

    async def start_recording(
        episode_id, device, rtsp_url, *, video_handler=None, codec_hint="", audio_codec_hint=""
    ):
        started.append((episode_id, device.id, rtsp_url))
        recorder._recordings[(episode_id, device.id)] = SimpleNamespace(
            episode_id=episode_id,
            device_id=device.id,
        )

    async def stop_recording(recording):
        stopped.append((recording.episode_id, recording.device_id))
        recorder._recordings.pop((recording.episode_id, recording.device_id), None)

    recorder._start_recording = start_recording
    recorder._stop_recording = stop_recording
    return started, stopped


@pytest.fixture
def config():
    tmpdir = tempfile.mkdtemp()
    return EpisodeConfig(
        data_dir=tmpdir,
        db_path=os.path.join(tmpdir, "test.db"),
        episode_timeout=2,
    )


@pytest.fixture
def repo(config):
    return Repository(config)


@pytest.fixture
def bus():
    return EventBus()


def _now():
    return datetime.now(tz=timezone.utc)


async def _add_areas(repo: Repository, *area_ids: str) -> None:
    for area_id in area_ids:
        await repo.upsert_area(Area(id=area_id, name=area_id))


async def _persist_episode(
    repo: Repository,
    episode_id: str,
    area_id: str,
    *,
    state: EpisodeState = EpisodeState.ACTIVE,
    minimum_end_at: datetime | None = None,
) -> Episode:
    if repo._conn is None:
        await repo.initialize()
    await _add_areas(repo, area_id)
    episode = Episode(
        id=episode_id,
        primary_area_id=area_id,
        minimum_end_at=minimum_end_at,
        state=state,
    )
    await repo.create_episode(episode)
    return episode


@pytest.mark.asyncio
async def test_recording_skips_non_video_device(repo, bus, config):
    await repo.initialize()
    await _add_areas(repo, "area-1")
    device = Device(
        id="device-no-video",
        name="Door Contact",
        device_type="contact",
        area_id="area-1",
        capabilities=[],
    )
    await repo.upsert_device(device)

    engine = EpisodeEngine(repo, bus, timeout=config.episode_timeout)
    recorder = RecordingEngine(repo, bus, config.data_dir)
    await engine.start()
    await recorder.start()

    await bus.publish(
        Message(
            type="event.received",
            data={
                "event": {
                    "device_id": "device-no-video",
                    "area_id": "area-1",
                    "timestamp": _now(),
                    "event_type": "contact_open",
                    "event_state": EventState.ACTIVE.value,
                    "source": "test",
                }
            },
        )
    )

    await asyncio.sleep(0.3)
    events = await repo.list_events()
    recordings = [e for e in await repo.list_evidence() if e.evidence_type == "recording"]
    assert len(recordings) == 0
    assert len(events) >= 1

    await recorder.stop()
    await engine.stop()
    await repo.close()


@pytest.mark.asyncio
async def test_recording_skips_video_device_without_url(repo, bus, config):
    await repo.initialize()
    await _add_areas(repo, "area-1")
    device = Device(
        id="device-video-no-url",
        name="Camera without config",
        device_type="hikvision",
        area_id="area-1",
        capabilities=["video"],
    )
    await repo.upsert_device(device)

    engine = EpisodeEngine(repo, bus, timeout=config.episode_timeout)
    recorder = RecordingEngine(repo, bus, config.data_dir)
    await engine.start()
    await recorder.start()

    await bus.publish(
        Message(
            type="event.received",
            data={
                "event": {
                    "device_id": "device-video-no-url",
                    "area_id": "area-1",
                    "timestamp": _now(),
                    "event_type": "motion_detection",
                    "event_state": EventState.ACTIVE.value,
                    "source": "test",
                }
            },
        )
    )

    await asyncio.sleep(0.1)
    recordings = [e for e in await repo.list_evidence() if e.evidence_type == "recording"]
    assert len(recordings) == 0

    await recorder.stop()
    await engine.stop()
    await repo.close()


@pytest.mark.asyncio
async def test_non_video_event_starts_area_episode_recordings(repo, bus, config):
    await repo.initialize()
    await _add_areas(repo, "area-1", "area-2")
    sensor = Device(
        id="ground-sensor",
        name="Ground sensor",
        device_type="sensor",
        area_id="area-1",
    )
    await repo.upsert_device(sensor)
    await repo.upsert_device(
        Device(
            id="camera-bad",
            name="camera-bad",
            device_type="camera",
            area_id="area-1",
            capabilities=["video"],
            configs={"video": CapabilityConfig(settings={"recording_mode": "on_episode"})},
        )
    )
    await repo.upsert_device(_video_device("camera-x", "area-1", "on_episode"))
    await repo.upsert_device(_video_device("doorbell", "area-1", "on_event"))
    await repo.upsert_device(_video_device("camera-other", "area-2", "on_episode"))

    engine = EpisodeEngine(repo, bus, timeout=config.episode_timeout)
    recorder = RecordingEngine(repo, bus, config.data_dir)
    started, stopped = _replace_recording_processes(recorder)
    await engine.start()
    await recorder.start()

    timestamp = _now()
    event_data = {
        "device_id": sensor.id,
        "area_id": sensor.area_id,
        "timestamp": timestamp,
        "event_type": "ground_contact",
        "event_state": EventState.ACTIVE.value,
        "source": "test",
    }
    await bus.publish(Message(type="event.received", data={"event": dict(event_data)}))

    episodes = await repo.list_episodes()
    assert len(episodes) == 1
    assert episodes[0].primary_area_id == "area-1"
    assert [(episode_id, device_id) for episode_id, device_id, _ in started] == [
        (episodes[0].id, "camera-x")
    ]

    await bus.publish(Message(type="event.received", data={"event": dict(event_data)}))
    assert len(started) == 1

    await bus.publish(
        Message(
            type="event.received",
            data={
                "event": {
                    **event_data,
                    "timestamp": timestamp,
                    "event_state": EventState.INACTIVE.value,
                }
            },
        )
    )
    assert len(started) == 1

    await bus.publish(
        Message(
            type="episode.updated",
            data={"episode_id": episodes[0].id, "state": "quiescent"},
        )
    )
    assert stopped == []

    await bus.publish(
        Message(
            type="episode.updated",
            data={"episode_id": episodes[0].id, "state": "closed"},
        )
    )
    assert stopped == [(episodes[0].id, "camera-x")]

    await recorder.stop()
    await engine.stop()
    await repo.close()


@pytest.mark.asyncio
async def test_doorbell_and_area_camera_share_episode_recording_lifecycle(repo, bus, config):
    await repo.initialize()
    await _add_areas(repo, "front-door")
    doorbell = _video_device("doorbell", "front-door", "on_event")
    camera = _video_device("camera-x", "front-door", "on_episode")
    await repo.upsert_device(doorbell)
    await repo.upsert_device(camera)

    engine = EpisodeEngine(repo, bus, timeout=config.episode_timeout)
    recorder = RecordingEngine(repo, bus, config.data_dir)
    started, stopped = _replace_recording_processes(recorder)
    await engine.start()
    await recorder.start()

    timestamp = _now()
    await bus.publish(
        Message(
            type="event.received",
            data={
                "event": {
                    "device_id": doorbell.id,
                    "area_id": doorbell.area_id,
                    "timestamp": timestamp,
                    "event_type": "doorbell",
                    "event_state": EventState.ACTIVE.value,
                    "source": "test:doorbell",
                }
            },
        )
    )

    episodes = await repo.list_episodes()
    assert len(episodes) == 1
    episode_id = episodes[0].id
    assert {(ep, device_id) for ep, device_id, _ in started} == {
        (episode_id, "doorbell"),
        (episode_id, "camera-x"),
    }

    await bus.publish(
        Message(
            type="event.received",
            data={
                "event": {
                    "device_id": camera.id,
                    "area_id": camera.area_id,
                    "timestamp": timestamp,
                    "event_type": "motion_detection",
                    "event_state": EventState.ACTIVE.value,
                    "source": "hikvision:isapi",
                }
            },
        )
    )

    episodes = await repo.list_episodes()
    assert len(episodes) == 1
    assert episodes[0].event_count == 2
    assert len(started) == 2
    assert set(recorder._recordings) == {
        (episode_id, "doorbell"),
        (episode_id, "camera-x"),
    }

    await bus.publish(
        Message(
            type="episode.updated",
            data={"episode_id": episode_id, "state": "closed"},
        )
    )
    assert set(stopped) == {
        (episode_id, "doorbell"),
        (episode_id, "camera-x"),
    }
    assert recorder._recordings == {}

    await recorder.stop()
    await engine.stop()
    await repo.close()


@pytest.mark.asyncio
async def test_same_prefix_devices_get_distinct_recording_paths(repo, bus, config):
    await _persist_episode(repo, "episode-1", "garagem")
    recorder = RecordingEngine(repo, bus, config.data_dir)
    release = asyncio.Event()

    async def hold_recording(recording, rtsp_url):
        await release.wait()

    recorder._record_episode = hold_recording

    await recorder._start_recording(
        "episode-1",
        _video_device("cam-garagem", "garagem", "on_event"),
        "rtsp://camera-garagem/stream",
    )
    await recorder._start_recording(
        "episode-1",
        _video_device("cam-garagem-interior", "garagem", "on_episode"),
        "rtsp://camera-garagem-interior/stream",
    )

    recordings = list(recorder._recordings.values())
    output_paths = {recording.output_path for recording in recordings}
    working_paths = {recording.working_path for recording in recordings}

    assert len(output_paths) == 2
    assert len(working_paths) == 2
    assert all(path.endswith("/index.m3u8") for path in output_paths)
    assert all(path.endswith("/segments/segment-%06d.m4s") for path in working_paths)
    assert all("/recordings/" in path for path in output_paths)

    release.set()
    await asyncio.gather(*(recording.task for recording in recordings))
    await recorder.stop()
    await repo.close()


@pytest.mark.asyncio
async def test_hls_fragments_remain_one_evidence_bundle(repo, bus, config):
    await _persist_episode(repo, "episode-1", "area-1")
    recorder = RecordingEngine(repo, bus, config.data_dir, fragment_seconds=60)
    release = asyncio.Event()
    published = []

    async def hold_recording(recording, rtsp_url):
        await release.wait()

    async def capture_evidence(msg):
        published.append(msg.data["evidence"])

    async def persisted_evidence(evidence_id):
        return published[0] if published and published[0]["id"] == evidence_id else None

    recorder._record_episode = hold_recording
    repo.get_evidence = persisted_evidence
    bus.subscribe("evidence.received", capture_evidence)

    await recorder._start_recording(
        "episode-1",
        _video_device("camera-x", "area-1", "on_episode"),
        "rtsp://camera-x/stream",
    )
    recording = recorder._recordings[("episode-1", "camera-x")]
    recording.bundle.playlist_path.write_text(
        '#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n'
        "#EXTINF:4.0,\nsegments/segment-000000.m4s\n"
        "#EXTINF:4.0,\nsegments/segment-000001.m4s\n",
        encoding="utf-8",
    )
    (recording.bundle.root / "init.mp4").write_bytes(b"init")
    for index in range(2):
        (recording.bundle.root / "segments" / f"segment-{index:06d}.m4s").write_bytes(
            bytes([index]) * 4096
        )

    recording.bundle.refresh_manifest(state="recording")
    assert published == []
    assert recording.bundle.next_segment_index() == 2

    await recorder._finalize_bundle(recording)

    assert len(published) == 1
    assert published[0]["id"] == recording.evidence_id
    assert published[0]["file_path"] == str(recording.bundle.playlist_path)
    assert published[0]["metadata"]["fragment_count"] == 2
    assert published[0]["metadata"]["fragment_seconds"] == 60

    release.set()
    await recording.task
    await recorder.stop()
    await repo.close()


@pytest.mark.asyncio
async def test_invalid_hls_playlist_is_published_as_incomplete_with_exit_code(tmp_path, bus):
    class JournalRepository:
        async def get_episode(self, episode_id):
            return Episode(id=episode_id, state=EpisodeState.ACTIVE)

        async def append_episode_journal(self, episode_id, entry_type, data):
            return None

    repository = JournalRepository()
    device = _video_device("camera-x", "area-1", "on_episode")
    release = asyncio.Event()
    published = []

    async def hold_recording(recording, rtsp_url):
        await release.wait()

    async def persist_evidence(evidence):
        published.append(evidence)
        return evidence

    recorder = RecordingEngine(
        repository,
        bus,
        str(tmp_path),
        evidence_sink=persist_evidence,
    )
    recorder._record_episode = hold_recording
    await recorder._start_recording(
        "episode-1",
        device,
        "rtsp://camera-x/stream",
    )
    recording = recorder._recordings[("episode-1", "camera-x")]
    (recording.bundle.root / "init.mp4").write_bytes(b"init")
    (recording.bundle.root / "segments" / "segment-000000.m4s").write_bytes(b"fragment")
    recording.bundle.playlist_path.write_bytes(b"")
    recording.last_exit_code = 1

    await recorder._finalize_bundle(recording)

    assert len(published) == 1
    evidence = published[0]
    assert evidence.evidence_type == "incomplete_recording"
    assert evidence.file_path == str(recording.bundle.component_manifest_path)
    assert evidence.mime_type == "application/json"
    assert evidence.metadata["reason"] == "invalid_hls_playlist"
    assert evidence.metadata["ffmpeg_exit_code"] == 1
    assert evidence.metadata["playlist_validation"]["error"] == "playlist_header_missing"
    assert recording.bundle.playlist_path.read_bytes() == b""

    release.set()
    await recording.task
    await recorder.stop()


@pytest.mark.asyncio
async def test_recorder_diagnostics_report_progress_without_stream_credentials(repo, bus, config):
    await repo.initialize()
    await _persist_episode(repo, "episode-1", "area-1")
    recorder = RecordingEngine(repo, bus, config.data_dir)
    release = asyncio.Event()

    async def hold_recording(recording, rtsp_url):
        await release.wait()

    recorder._record_episode = hold_recording
    await recorder.start()
    await recorder._start_recording(
        "episode-1",
        _video_device("camera-x", "area-1", "on_episode"),
        "rtsp://private-user:private-password@camera-x/stream",
    )
    recording = recorder._recordings[("episode-1", "camera-x")]
    (recording.bundle.root / "segments" / "segment-000000.m4s").write_bytes(b"fragment")
    recording.bundle.playlist_path.write_text(
        "#EXTM3U\n#EXTINF:4.0,\nsegments/segment-000000.m4s\n",
        encoding="utf-8",
    )

    status = recorder.status()
    diagnostic = status["recordings"][0]
    assert status["state"] == "healthy"
    assert diagnostic["state"] == "recording"
    assert diagnostic["fragment_count"] == 1
    assert diagnostic["last_fragment_at"] is not None
    assert diagnostic["ready"] is False
    assert "rtsp" not in str(diagnostic)
    assert "private-password" not in str(status)

    (recording.bundle.root / "init.mp4").write_bytes(b"init")
    recording.bundle.playlist_path.write_text(
        '#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:4.0,\nsegments/segment-000000.m4s\n',
        encoding="utf-8",
    )
    assert recorder.status()["recordings"][0]["ready"] is True

    recording.state = "reconnecting"
    recording.last_error = "FFmpeg exited with code 1; reconnecting"
    status = recorder.status()
    assert status["state"] == "degraded"
    assert status["recordings"][0]["last_error"] == recording.last_error

    release.set()
    await recording.task
    await recorder.stop()
    await repo.close()


@pytest.mark.asyncio
async def test_diagnostic_progress_polling_does_not_hide_watchdog_progress(
    repo, bus, config, monkeypatch
):
    await _persist_episode(repo, "episode-watchdog-poll", "area-1")
    device = _video_device("camera-x", "area-1", "on_episode")
    await repo.upsert_device(device)
    recorder = RecordingEngine(repo, bus, config.data_dir)
    recorder._stall_seconds = 0.1
    first_fragment = asyncio.Event()
    processes = []

    class StreamingProcess:
        def __init__(self, root: Path):
            self.returncode = None
            self.stderr = None
            self.finished = asyncio.Event()
            self.writer_task = asyncio.create_task(self._write_fragments(root))

        async def _write_fragments(self, root: Path):
            index = 0
            segments = root / "segments"
            while self.returncode is None:
                (segments / f"segment-{index:06d}.m4s").write_bytes(b"fragment")
                first_fragment.set()
                index += 1
                await asyncio.sleep(0.02)

        def terminate(self):
            if self.returncode is None:
                self.returncode = -15
                self.finished.set()

        def kill(self):
            if self.returncode is None:
                self.returncode = -9
                self.finished.set()

        async def wait(self):
            await self.finished.wait()
            await self.writer_task
            return self.returncode

    def start_process(*args, **kwargs):
        process = StreamingProcess(Path(kwargs["cwd"]))
        processes.append(process)
        return process

    monkeypatch.setattr(recorder, "_spawn_ffmpeg", start_process)
    await recorder.start()
    await recorder._start_recording(
        "episode-watchdog-poll",
        device,
        "rtsp://camera-x/stream",
    )
    recording = recorder._recordings[("episode-watchdog-poll", device.id)]
    try:
        await asyncio.wait_for(first_fragment.wait(), timeout=1)

        deadline = asyncio.get_running_loop().time() + 1.25
        while asyncio.get_running_loop().time() < deadline:
            recorder.status()
            await asyncio.sleep(0.01)

        status = recorder.status()
        assert recording.fragment_count > 0
        assert status["stalled_recordings"] == 0
        assert status["recordings"][0]["state"] == "recording"
        assert len(processes) == 1
        assert processes[0].returncode is None
    finally:
        await recorder.stop()
        await repo.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [EpisodeState.FINALIZING, EpisodeState.CLOSED])
async def test_delayed_canonical_event_does_not_start_terminal_episode_recording(
    repo, bus, config, monkeypatch, state
):
    episode = await _persist_episode(repo, "episode-terminal", "area-1", state=state)
    device = _video_device("camera-x", "area-1", "on_episode")
    await repo.upsert_device(device)
    recorder = RecordingEngine(repo, bus, config.data_dir)
    started = []

    def spawn_ffmpeg(*args, **kwargs):
        started.append((args, kwargs))
        raise AssertionError("terminal Episodes must not spawn FFmpeg")

    monkeypatch.setattr(recorder, "_spawn_ffmpeg", spawn_ffmpeg)
    await recorder.start()
    event = Event(
        device_id=device.id,
        area_id=device.area_id,
        timestamp=_now(),
        event_type="motion_detection",
        event_state=EventState.ACTIVE,
        source="test:delayed-recorder-event",
        episode_id=episode.id,
    )

    await recorder._on_event(
        Message(
            type="event.canonicalized",
            data={"result": CanonicalEventResult(event=event, created=True)},
        )
    )
    await asyncio.sleep(0)

    assert started == []
    assert recorder._recordings == {}
    assert episode.id in recorder._terminal_episode_ids

    await recorder.stop()
    await repo.close()


@pytest.mark.asyncio
async def test_finalization_stops_partial_startup_writer_and_marks_incomplete(
    repo, bus, config, monkeypatch
):
    now = _now()
    episode = await _persist_episode(
        repo,
        "episode-start-race",
        "area-1",
        state=EpisodeState.QUIESCENT,
        minimum_end_at=now - timedelta(seconds=30),
    )
    device = _video_device("camera-x", "area-1", "on_episode")
    await repo.upsert_device(device)

    lifecycle_lock = asyncio.Lock()
    engine = EpisodeEngine(
        repo,
        bus,
        timeout=config.episode_timeout,
        quiescent_grace_seconds=1,
        lifecycle_lock=lifecycle_lock,
    )
    recorder = RecordingEngine(
        repo,
        bus,
        config.data_dir,
        lifecycle_lock=lifecycle_lock,
    )
    recorder.set_evidence_sink(engine.ingest_recording_evidence)
    writer_started = asyncio.Event()
    processes = []

    class PartialWriterProcess:
        def __init__(self, root: Path):
            self.returncode = None
            self.stderr = None
            self.terminate_calls = 0
            self.finished = asyncio.Event()
            self.partial_path = root / "segments" / "segment-000000.m4s.tmp"
            self.writer_task = asyncio.create_task(self._write_partial())

        async def _write_partial(self):
            size = 1
            while self.returncode is None:
                self.partial_path.write_bytes(b"x" * size)
                writer_started.set()
                size += 1
                await asyncio.sleep(0.01)

        def terminate(self):
            if self.returncode is None:
                self.terminate_calls += 1
                self.returncode = -15
                self.finished.set()

        def kill(self):
            if self.returncode is None:
                self.returncode = -9
                self.finished.set()

        async def wait(self):
            await self.finished.wait()
            await self.writer_task
            return self.returncode

    def spawn_partial_writer(_command, **kwargs):
        process = PartialWriterProcess(Path(kwargs["cwd"]))
        processes.append(process)
        return process

    monkeypatch.setattr(recorder, "_spawn_ffmpeg", spawn_partial_writer)
    await recorder.start()
    await recorder._start_recording(episode.id, device, "rtsp://camera-x/stream")
    recording = recorder._recordings[(episode.id, device.id)]
    finalization_task = None

    try:
        await asyncio.wait_for(writer_started.wait(), timeout=1)
        assert recording.proc is processes[0]
        finalization_task = asyncio.create_task(engine._close_timed_out_episodes(now=now))
        await asyncio.wait_for(finalization_task, timeout=2)

        assert (await repo.get_episode(episode.id)).state == EpisodeState.CLOSED
        assert recorder._recordings == {}
        process = processes[0]
        assert process.returncode is not None
        assert process.terminate_calls == 1
        await asyncio.wait_for(process.writer_task, timeout=1)

        evidence = await repo.get_evidence(recording.evidence_id)
        assert evidence is not None
        assert evidence.evidence_type == "incomplete_recording"
        assert evidence.metadata["status"] == "incomplete"
        component_manifest = json.loads(recording.bundle.component_manifest_path.read_text())
        assert component_manifest["state"] == "incomplete"
        assert component_manifest["playlist_validation"]["valid"] is False

        def capture_files():
            return {
                path.relative_to(recording.bundle.root).as_posix(): (
                    path.stat().st_size,
                    path.stat().st_mtime_ns,
                )
                for path in recording.bundle.root.rglob("*")
                if path.is_file()
            }

        snapshot = capture_files()
        await asyncio.sleep(0.03)
        assert capture_files() == snapshot
    finally:
        if finalization_task and not finalization_task.done():
            finalization_task.cancel()
            await asyncio.gather(finalization_task, return_exceptions=True)
        for process in processes:
            process.terminate()
        if recording.task and not recording.task.done():
            recording.task.cancel()
            await asyncio.gather(recording.task, return_exceptions=True)
        await recorder.stop()
        await repo.close()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="requires POSIX asyncio subprocess transport")
async def test_finalization_reaps_real_child_after_synchronous_process_handoff(
    repo, bus, config, monkeypatch
):
    proc_fds = Path("/proc/self/fd")
    if not proc_fds.is_dir():
        pytest.skip("requires /proc to verify file descriptor cleanup")
    now = _now()
    episode = await _persist_episode(
        repo,
        "episode-real-start-race",
        "area-1",
        state=EpisodeState.QUIESCENT,
        minimum_end_at=now - timedelta(seconds=30),
    )
    device = _video_device("camera-x", "area-1", "on_episode")
    await repo.upsert_device(device)
    lifecycle_lock = asyncio.Lock()
    engine = EpisodeEngine(
        repo,
        bus,
        timeout=config.episode_timeout,
        quiescent_grace_seconds=1,
        lifecycle_lock=lifecycle_lock,
    )
    recorder = RecordingEngine(
        repo,
        bus,
        config.data_dir,
        lifecycle_lock=lifecycle_lock,
    )
    recorder.set_evidence_sink(engine.ingest_recording_evidence)
    finalizer_started = asyncio.Event()

    async def track_finalizer(episode_id):
        finalizer_started.set()
        await recorder.finalize_episode(episode_id)

    engine.set_finalizer(track_finalizer)
    startup_entered = asyncio.Event()
    real_spawn_ffmpeg = recorder._spawn_ffmpeg
    processes = []

    def capture_process_handoff(*args, **kwargs):
        process = real_spawn_ffmpeg(*args, **kwargs)
        processes.append(process)
        startup_entered.set()
        return process

    monkeypatch.setattr(recorder, "_spawn_ffmpeg", capture_process_handoff)
    monkeypatch.setattr(
        recorder,
        "_rtsp_command",
        lambda _recording, _url, _flags: [
            sys.executable,
            "-c",
            "\n".join(
                [
                    "from pathlib import Path",
                    "import signal",
                    "import time",
                    "p = Path('segments/segment-000000.m4s.tmp')",
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
                    "size = 1",
                    "while True:",
                    "    p.write_bytes(b'x' * size)",
                    "    size += 1",
                    "    time.sleep(0.01)",
                ]
            ),
        ],
    )
    monkeypatch.setattr("episode.recording.engine.PROCESS_STOP_TIMEOUT_SECONDS", 0.5)
    await recorder.start()
    await recorder._start_recording(episode.id, device, "rtsp://camera-x/stream")
    recording = recorder._recordings[(episode.id, device.id)]
    finalization_task = None

    try:
        await asyncio.wait_for(startup_entered.wait(), timeout=2)
        process = processes[0]
        pid = process.pid
        assert recording.proc is process
        assert process.returncode is None
        stderr_fd = process.stderr.fileno()
        stderr_pipe = os.readlink(f"/proc/self/fd/{stderr_fd}")
        assert stderr_pipe.startswith("pipe:[")
        os.kill(pid, 0)
        partial = recording.bundle.root / "segments" / "segment-000000.m4s.tmp"
        for _ in range(100):
            if partial.exists() and partial.stat().st_size:
                break
            await asyncio.sleep(0.01)
        assert partial.exists() and partial.stat().st_size > 0

        finalization_task = asyncio.create_task(engine._close_timed_out_episodes(now=now))
        await asyncio.wait_for(finalizer_started.wait(), timeout=1)
        assert recording.finalize_requested is True
        assert (await repo.get_episode(episode.id)).state == EpisodeState.FINALIZING
        assert process.returncode is None
        size_before_stop = partial.stat().st_size
        await asyncio.sleep(0.05)
        assert process.returncode is None
        assert partial.stat().st_size > size_before_stop
        assert (await repo.get_episode(episode.id)).state == EpisodeState.FINALIZING
        await asyncio.wait_for(finalization_task, timeout=3)

        assert process.returncode is not None
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert process.stderr.closed
        open_pipe_targets = []
        for fd_path in proc_fds.iterdir():
            try:
                target = os.readlink(fd_path)
            except OSError:
                continue
            if target.startswith("pipe:["):
                open_pipe_targets.append(target)
        assert stderr_pipe not in open_pipe_targets
        assert recording.task is not None and recording.task.done()
        assert recorder._recordings == {}
        assert (await repo.get_episode(episode.id)).state == EpisodeState.CLOSED
        evidence = await repo.get_evidence(recording.evidence_id)
        assert evidence is not None
        assert evidence.evidence_type == "incomplete_recording"
        assert evidence.metadata["status"] == "incomplete"
        final_files = {
            path.relative_to(recording.bundle.root).as_posix(): (
                path.stat().st_size,
                path.stat().st_mtime_ns,
            )
            for path in recording.bundle.root.rglob("*")
            if path.is_file()
        }
        await asyncio.sleep(0.05)
        assert {
            path.relative_to(recording.bundle.root).as_posix(): (
                path.stat().st_size,
                path.stat().st_mtime_ns,
            )
            for path in recording.bundle.root.rglob("*")
            if path.is_file()
        } == final_files
    finally:
        if finalization_task and not finalization_task.done():
            finalization_task.cancel()
            await asyncio.gather(finalization_task, return_exceptions=True)
        for process in processes:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(asyncio.shield(process.wait()), timeout=0.5)
                except asyncio.TimeoutError:
                    process.kill()
                    await asyncio.wait_for(asyncio.shield(process.wait()), timeout=2)
        if recording.task and not recording.task.done():
            recording.task.cancel()
            await asyncio.gather(recording.task, return_exceptions=True)
        await recorder.stop()
        await repo.close()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="requires /proc to verify file descriptor cleanup")
async def test_application_shutdown_reaps_real_child_and_preserves_interrupted_workspace(
    repo, bus, config, monkeypatch
):
    proc_fds = Path("/proc/self/fd")
    if not proc_fds.is_dir():
        pytest.skip("requires /proc to verify file descriptor cleanup")
    episode = await _persist_episode(repo, "episode-real-shutdown", "area-1")
    device = _video_device("camera-x", "area-1", "on_episode")
    await repo.upsert_device(device)
    recorder = RecordingEngine(repo, bus, config.data_dir)
    monkeypatch.setattr(
        recorder,
        "_rtsp_command",
        lambda _recording, _url, _flags: [
            sys.executable,
            "-c",
            "\n".join(
                [
                    "from pathlib import Path",
                    "import time",
                    "p = Path('segments/segment-000000.m4s.tmp')",
                    "size = 1",
                    "while True:",
                    "    p.write_bytes(b'x' * size)",
                    "    size += 1",
                    "    time.sleep(0.01)",
                ]
            ),
        ],
    )
    await recorder.start()
    await recorder._start_recording(episode.id, device, "rtsp://camera-x/stream")
    recording = recorder._recordings[(episode.id, device.id)]

    process = None
    stderr_pipe = None
    try:
        for _ in range(100):
            process = recording.proc
            if process is not None:
                break
            await asyncio.sleep(0.01)
        assert process is not None
        assert process.returncode is None
        stderr_pipe = os.readlink(f"/proc/self/fd/{process.stderr.fileno()}")
        partial = recording.bundle.root / "segments" / "segment-000000.m4s.tmp"
        for _ in range(100):
            if partial.exists() and partial.stat().st_size:
                break
            await asyncio.sleep(0.01)
        assert partial.exists() and partial.stat().st_size > 0

        await recorder.stop()

        assert process.returncode is not None
        with pytest.raises(ProcessLookupError):
            os.kill(process.pid, 0)
        assert process.stderr.closed
        stopped_files = {
            path.relative_to(recording.bundle.root).as_posix(): path.stat().st_size
            for path in recording.bundle.root.rglob("*")
            if path.is_file()
        }
        assert stopped_files
        await asyncio.sleep(0.05)
        assert {
            path.relative_to(recording.bundle.root).as_posix(): path.stat().st_size
            for path in recording.bundle.root.rglob("*")
            if path.is_file()
        } == stopped_files
        open_pipe_targets = []
        for fd_path in proc_fds.iterdir():
            try:
                target = os.readlink(fd_path)
            except OSError:
                continue
            if target.startswith("pipe:["):
                open_pipe_targets.append(target)
        assert stderr_pipe not in open_pipe_targets
        assert await repo.get_evidence(recording.evidence_id) is None
        manifest = json.loads(recording.bundle.component_manifest_path.read_text())
        assert manifest["state"] == "interrupted"
        assert manifest["reason"] == "application_shutdown"

        recovered_recorder = RecordingEngine(repo, bus, config.data_dir)
        await recovered_recorder.recover_interrupted_recordings()
        recovered = recovered_recorder._recoverable[(episode.id, device.id)]
        assert recovered.bundle.state.session_id == recording.session_id
        assert any(
            Path(path).name == "segment-000000.m4s.tmp"
            for path in recovered_recorder.active_file_paths()
        )
    finally:
        if recorder._running:
            await recorder.stop()
        await repo.close()


@pytest.mark.asyncio
async def test_active_fragment_progress_is_incremental_and_final_inventory_is_complete(
    repo, bus, config, monkeypatch
):
    await _persist_episode(repo, "episode-progress", "area-1")
    published = []

    async def persist_evidence(evidence):
        published.append(evidence)
        return evidence

    recorder = RecordingEngine(
        repo,
        bus,
        config.data_dir,
        evidence_sink=persist_evidence,
    )
    release = asyncio.Event()

    async def hold_recording(recording, rtsp_url):
        await release.wait()

    recorder._record_episode = hold_recording
    await recorder.start()
    await recorder._start_recording(
        "episode-progress",
        _video_device("camera-x", "area-1", "on_episode"),
        "rtsp://camera-x/stream",
    )
    recording = recorder._recordings[("episode-progress", "camera-x")]
    segments_dir = recording.bundle.root / "segments"
    original_is_file = Path.is_file
    checked_segments = []

    def count_segment_checks(path: Path):
        if path.parent == segments_dir:
            checked_segments.append(path.name)
        return original_is_file(path)

    monkeypatch.setattr(Path, "is_file", count_segment_checks)
    monkeypatch.setattr(
        recording.bundle,
        "next_segment_index",
        lambda: pytest.fail("active progress must not rescan all fragments"),
    )

    fragment_count = 200
    for index in range(fragment_count):
        (segments_dir / f"segment-{index:06d}.m4s").write_bytes(b"fragment")

    first_status = recorder.status()["recordings"][0]
    assert first_status["fragment_count"] == fragment_count
    assert len(checked_segments) == fragment_count + 1

    checked_segments.clear()
    (segments_dir / f"segment-{fragment_count:06d}.m4s").write_bytes(b"fragment")
    assert recorder.status()["recordings"][0]["fragment_count"] == fragment_count + 1
    assert checked_segments == [
        f"segment-{fragment_count:06d}.m4s",
        f"segment-{fragment_count + 1:06d}.m4s",
    ]

    (recording.bundle.root / "init.mp4").write_bytes(b"init")
    playlist = [
        "#EXTM3U",
        '#EXT-X-MAP:URI="init.mp4"',
    ]
    for index in range(fragment_count + 1):
        playlist.extend(
            [
                "#EXTINF:4.0,",
                f"segments/segment-{index:06d}.m4s",
            ]
        )
    recording.bundle.playlist_path.write_text("\n".join(playlist) + "\n", encoding="utf-8")

    await recorder._finalize_bundle(recording)

    assert len(published) == 1
    assert published[0].evidence_type == "recording"
    assert published[0].metadata["fragment_count"] == fragment_count + 1
    component_manifest = json.loads(recording.bundle.component_manifest_path.read_text())
    segments = [
        item for item in component_manifest["components"] if item["kind"] == "media_segment"
    ]
    assert component_manifest["state"] == "complete"
    assert component_manifest["fragment_count"] == fragment_count + 1
    assert len(segments) == fragment_count + 1
    assert {item["sequence"] for item in segments} == set(range(fragment_count + 1))
    assert component_manifest["playlist_validation"]["valid"] is True

    release.set()
    await recording.task
    await recorder.stop()
    await repo.close()


@pytest.mark.asyncio
async def test_active_manifest_refresh_timer_spans_short_subprocess_retries(
    repo, bus, config, monkeypatch
):
    episode = await _persist_episode(repo, "episode-manifest-retry", "area-1")
    device = _video_device("camera-x", "area-1", "on_episode")
    await repo.upsert_device(device)
    monkeypatch.setattr(
        "episode.recording.engine.ACTIVE_MANIFEST_REFRESH_SECONDS",
        0.1,
    )

    loop = asyncio.get_running_loop()
    refresh_enabled = False
    refresh_completed = asyncio.Event()
    active_refreshes = []
    original_refresh = HLSRecordingBundle.refresh_manifest

    def track_refresh(bundle, *, state, ended_at=None, reason=None):
        manifest = original_refresh(
            bundle,
            state=state,
            ended_at=ended_at,
            reason=reason,
        )
        if refresh_enabled and bundle.state.episode_id == episode.id and state == "recording":
            active_refreshes.append(True)
            loop.call_soon_threadsafe(refresh_completed.set)
        return manifest

    monkeypatch.setattr(HLSRecordingBundle, "refresh_manifest", track_refresh)
    attempts = []

    class ShortAttempt:
        def __init__(self):
            self.returncode = None
            self.stderr = None
            self.finished = asyncio.Event()
            asyncio.create_task(self._finish_shortly())

        async def _finish_shortly(self):
            await asyncio.sleep(0.06)
            if self.returncode is None:
                self.returncode = 0
                self.finished.set()

        def terminate(self):
            if self.returncode is None:
                self.returncode = -15
                self.finished.set()

        def kill(self):
            if self.returncode is None:
                self.returncode = -9
                self.finished.set()

        async def wait(self):
            await self.finished.wait()
            return self.returncode

    def short_attempt(*args, **kwargs):
        attempt = len(attempts)
        attempts.append(attempt)
        segments = Path(kwargs["cwd"]) / "segments"
        (segments / f"segment-{attempt:06d}.m4s").write_bytes(b"fragment")
        return ShortAttempt()

    recorder = RecordingEngine(repo, bus, config.data_dir)
    monkeypatch.setattr(recorder, "_spawn_ffmpeg", short_attempt)
    recorder._retry_initial_seconds = 0.01
    recorder._retry_max_seconds = 0.01
    await recorder.start()
    await recorder._start_recording(
        episode.id,
        device,
        "rtsp://camera-x/stream",
    )
    recording = recorder._recordings[(episode.id, device.id)]
    refresh_enabled = True

    try:
        await asyncio.wait_for(refresh_completed.wait(), timeout=1)
        assert len(attempts) >= 2
        assert active_refreshes
        active_manifest = json.loads(recording.bundle.component_manifest_path.read_text())
        assert active_manifest["state"] == "recording"
        assert active_manifest["fragment_count"] >= 2
    finally:
        await recorder.stop()
        await repo.close()


@pytest.mark.asyncio
async def test_stalled_stream_reconnect_gap_still_finalizes_on_episode_close(
    repo, bus, config, monkeypatch
):
    await repo.initialize()
    await _add_areas(repo, "area-1")
    device = _video_device("camera-x", "area-1", "on_episode")
    await repo.upsert_device(device)
    await repo.create_episode(
        Episode(id="episode-1", primary_area_id="area-1", state=EpisodeState.ACTIVE)
    )
    recorder = RecordingEngine(repo, bus, config.data_dir)
    recorder._stall_seconds = 0.01
    terminated = asyncio.Event()

    class StalledProcess:
        def __init__(self):
            self.returncode = None
            self.stderr = None

        def terminate(self):
            self.returncode = -15
            terminated.set()

        def kill(self):
            self.returncode = -9
            terminated.set()

        async def wait(self):
            await terminated.wait()
            return self.returncode

    def start_ffmpeg(*args, **kwargs):
        return StalledProcess()

    async def persist_evidence(message):
        await repo.create_evidence(Evidence(**message.data["evidence"]))

    monkeypatch.setattr(recorder, "_spawn_ffmpeg", start_ffmpeg)
    bus.subscribe("evidence.received", persist_evidence)
    await recorder.start()
    await recorder._start_recording(
        "episode-1",
        device,
        "rtsp://camera-x/stream",
    )
    recording = recorder._recordings[("episode-1", "camera-x")]

    await asyncio.wait_for(terminated.wait(), timeout=2)
    for _attempt in range(50):
        if recorder.status()["reconnects"]:
            break
        await asyncio.sleep(0.01)

    status = recorder.status()
    assert status["stalled_recordings"] == 1
    assert status["reconnects"] == 1
    assert status["state"] == "degraded"

    await recorder._stop_recording(recording)
    evidence = await repo.get_evidence(recording.evidence_id)
    assert evidence is not None
    assert evidence.evidence_type == "incomplete_recording"
    assert not recording.bundle.capture_state_path.exists()

    await recorder.stop()
    await repo.close()


@pytest.mark.asyncio
async def test_hls_recovery_marker_survives_missing_persistence_ack(repo, bus, config):
    await _persist_episode(repo, "episode-1", "area-1")
    recorder = RecordingEngine(repo, bus, config.data_dir)
    release = asyncio.Event()

    async def hold_recording(recording, rtsp_url):
        await release.wait()

    async def missing_evidence(evidence_id):
        return None

    recorder._record_episode = hold_recording
    repo.get_evidence = missing_evidence
    await recorder._start_recording(
        "episode-1",
        _video_device("camera-x", "area-1", "on_episode"),
        "rtsp://camera-x/stream",
    )
    recording = recorder._recordings[("episode-1", "camera-x")]
    recording.bundle.playlist_path.write_text(
        "#EXTM3U\n#EXTINF:4.0,\nsegments/segment-000000.m4s\n",
        encoding="utf-8",
    )
    (recording.bundle.root / "segments" / "segment-000000.m4s").write_bytes(b"fragment")

    with pytest.raises(RuntimeError, match="recovery marker has been retained"):
        await recorder._finalize_bundle(recording)

    assert recording.bundle.capture_state_path.exists()
    assert recording.published is False
    release.set()
    await recording.task
    await recorder.stop()
    await repo.close()


@pytest.mark.asyncio
async def test_application_shutdown_leaves_active_hls_bundle_recoverable(
    repo, bus, config, monkeypatch
):
    await _persist_episode(repo, "episode-1", "area-1")
    recorder = RecordingEngine(repo, bus, config.data_dir, fragment_seconds=60)
    process_started = asyncio.Event()
    process_finished = asyncio.Event()
    published = []

    class ActiveProcess:
        returncode = None
        stderr = None

        def terminate(self):
            self.returncode = 0
            process_finished.set()

        def kill(self):
            self.returncode = -9
            process_finished.set()

        async def wait(self):
            await process_finished.wait()
            return self.returncode

    def start_ffmpeg(*args, **kwargs):
        root = Path(kwargs["cwd"])
        (root / "segments" / "segment-000000.m4s").write_bytes(b"video" * 1024)
        (root / "index.m3u8").write_text(
            "#EXTM3U\n#EXTINF:4,\nsegments/segment-000000.m4s\n",
            encoding="utf-8",
        )
        process_started.set()
        return ActiveProcess()

    async def capture_evidence(message):
        published.append(message.data["evidence"])

    monkeypatch.setattr(recorder, "_spawn_ffmpeg", start_ffmpeg)
    bus.subscribe("evidence.received", capture_evidence)
    await recorder.start()

    await recorder._start_recording(
        "episode-1",
        _video_device("camera-x", "area-1", "on_episode"),
        "rtsp://camera-x/stream",
    )
    await process_started.wait()
    await recorder.stop()

    assert recorder._recordings == {}
    assert published == []
    capture_states = list(
        Path(config.data_dir).glob("episodes/episode-1/recordings/*/capture.json")
    )
    assert len(capture_states) == 1
    await repo.close()


@pytest.mark.asyncio
async def test_multicamera_shutdown_signals_all_processes_before_waiting(
    repo, bus, config, monkeypatch
):
    await _persist_episode(repo, "episode-1", "area-1")
    recorder = RecordingEngine(repo, bus, config.data_dir, fragment_seconds=60)
    all_terminated = asyncio.Event()
    processes = {}
    published = []

    class ActiveProcess:
        def __init__(self, device_id):
            self.device_id = device_id
            self.returncode = None
            self.killed = False
            self.waited_after_all_signals = False
            self.stderr = None

        def terminate(self):
            self.returncode = 0
            if all(process.returncode is not None for process in processes.values()):
                all_terminated.set()

        def kill(self):
            self.killed = True
            self.returncode = -9
            all_terminated.set()

        async def wait(self):
            await all_terminated.wait()
            self.waited_after_all_signals = all(
                process.returncode is not None for process in processes.values()
            )
            return self.returncode

    def start_ffmpeg(*args, **kwargs):
        device_id = f"camera-{'a' if not processes else 'b'}"
        process = ActiveProcess(device_id)
        processes[device_id] = process
        return process

    async def capture_evidence(message):
        published.append(message.data["evidence"])

    monkeypatch.setattr(recorder, "_spawn_ffmpeg", start_ffmpeg)
    bus.subscribe("evidence.received", capture_evidence)
    await recorder.start()
    await recorder._start_recording(
        "episode-1",
        _video_device("camera-a", "area-1", "on_episode"),
        "rtsp://camera-a/stream",
    )
    await recorder._start_recording(
        "episode-1",
        _video_device("camera-b", "area-1", "on_episode"),
        "rtsp://camera-b/stream",
    )
    while len(processes) < 2:
        await asyncio.sleep(0)

    await asyncio.wait_for(recorder.stop(), timeout=1)

    assert set(processes) == {"camera-a", "camera-b"}
    assert all(process.waited_after_all_signals for process in processes.values())
    assert not any(process.killed for process in processes.values())
    assert published == []
    await repo.close()


@pytest.mark.asyncio
async def test_startup_reconciles_playable_and_invalid_recording_partials(
    repo, bus, config, monkeypatch
):
    await repo.initialize()
    await _add_areas(repo, "area-1")
    await repo.upsert_device(_video_device("camera-x", "area-1", "on_event"))
    episode = Episode(
        id="episode-1",
        primary_area_id="area-1",
        state=EpisodeState.FINALIZING,
    )
    await repo.create_episode(episode)
    recordings_dir = os.path.join(config.data_dir, "episodes", episode.id, "recordings")
    os.makedirs(recordings_dir, exist_ok=True)
    valid_part = os.path.join(
        recordings_dir,
        "rec_camera-x_20260824_120000_000000_aaaaaaaaaaaa_000000.mp4.part",
    )
    invalid_part = os.path.join(
        recordings_dir,
        "rec_camera-x_20260824_120001_000000_bbbbbbbbbbbb_000000.mp4.part",
    )
    for path in (valid_part, invalid_part):
        with open(path, "wb") as segment:
            segment.write(b"video" * 1024)

    async def valid_video(path):
        return path == valid_part

    monkeypatch.setattr(
        RecordingEngine,
        "_has_video_stream",
        lambda self, path: valid_video(path),
    )
    engine = EpisodeEngine(repo, bus, timeout=config.episode_timeout)
    recorder = RecordingEngine(repo, bus, config.data_dir)
    recorder.set_evidence_sink(engine.ingest_recording_evidence)
    await engine.start(defer_finalization=True)
    await recorder.start()
    await recorder.recover_interrupted_recordings()

    evidence = await repo.list_evidence(episode_id=episode.id, limit=10)
    by_type = {item.evidence_type: item for item in evidence}
    assert set(by_type) == {"recording", "incomplete_recording"}
    assert os.path.exists(by_type["recording"].file_path)
    assert by_type["recording"].metadata["recording_session_id"] == "aaaaaaaaaaaa"
    assert os.path.exists(by_type["incomplete_recording"].file_path)
    assert by_type["incomplete_recording"].metadata["reason"] == "startup_recovery"
    assert not os.path.exists(valid_part)
    assert not os.path.exists(invalid_part)

    journal_path = os.path.join(config.data_dir, "episodes", episode.id, "journal.ndjson")
    with open(journal_path, encoding="utf-8") as journal:
        journal_types = [json.loads(line)["type"] for line in journal]
    assert "recording.recovered" in journal_types
    assert "recording.incomplete" in journal_types

    await recorder.stop()
    await engine.stop()
    await repo.close()


@pytest.mark.asyncio
async def test_startup_ignores_recording_partials_under_closed_episodes(repo, bus, config):
    await repo.initialize()
    await _add_areas(repo, "area-1")
    await repo.upsert_device(_video_device("camera-x", "area-1", "on_event"))
    episode = Episode(id="sealed-episode", primary_area_id="area-1", state=EpisodeState.CLOSED)
    await repo.create_episode(episode)
    partial = (
        Path(config.data_dir)
        / "episodes"
        / episode.id
        / "recordings"
        / "rec_camera-x_20260824_120000_000000_aaaaaaaaaaaa_000000.mp4.part"
    )
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"sealed working output")

    recorder = RecordingEngine(repo, bus, config.data_dir)
    await recorder.start()
    await recorder.recover_interrupted_recordings()

    assert partial.exists()
    assert await repo.list_evidence(episode_id=episode.id, limit=10) == []
    await recorder.stop()
    await repo.close()


@pytest.mark.asyncio
async def test_startup_defers_recovered_bundle_to_retryable_episode_finalizer(repo, bus, config):
    await repo.initialize()
    await _add_areas(repo, "area-1")
    await repo.upsert_device(_video_device("camera-x", "area-1", "on_event"))
    episode = Episode(
        id="episode-finalizing",
        primary_area_id="area-1",
        state=EpisodeState.FINALIZING,
    )
    await repo.create_episode(episode)
    root = Path(config.data_dir, "episodes", episode.id, "recordings", "evidence-1")
    bundle = HLSRecordingBundle.create(
        root,
        HLSCaptureState(
            evidence_id="evidence-1",
            episode_id=episode.id,
            device_id="camera-x",
            area_id="area-1",
            session_id="aaaaaaaaaaaa",
            started_at=_now() - timedelta(seconds=10),
        ),
    )
    bundle.playlist_path.write_text(
        "#EXTM3U\n#EXTINF:4,\nsegments/segment-000000.m4s\n",
        encoding="utf-8",
    )
    (bundle.root / "segments" / "segment-000000.m4s").write_bytes(b"video")

    async def fail_publication(_evidence):
        raise RuntimeError("database unavailable")

    recorder = RecordingEngine(
        repo,
        bus,
        config.data_dir,
        evidence_sink=fail_publication,
    )
    await recorder.start()

    await recorder.recover_interrupted_recordings()

    assert (episode.id, "camera-x") in recorder._recoverable
    assert bundle.capture_state_path.exists()
    assert str(bundle.capture_state_path) in recorder.active_file_paths()
    with pytest.raises(RuntimeError, match="database unavailable"):
        await recorder.finalize_episode(episode.id)
    assert (episode.id, "camera-x") in recorder._recoverable
    assert (await repo.get_episode(episode.id)).state == EpisodeState.FINALIZING

    await recorder.stop()
    await repo.close()


@pytest.mark.asyncio
async def test_incomplete_finalization_survives_failed_publication_and_restart(repo, bus, config):
    await repo.initialize()
    await _add_areas(repo, "area-1")
    await repo.upsert_device(_video_device("camera-x", "area-1", "on_event"))
    episode = Episode(
        id="episode-incomplete-retry",
        primary_area_id="area-1",
        state=EpisodeState.FINALIZING,
    )
    await repo.create_episode(episode)
    root = Path(config.data_dir, "episodes", episode.id, "recordings", "evidence-incomplete")
    bundle = HLSRecordingBundle.create(
        root,
        HLSCaptureState(
            evidence_id="evidence-incomplete",
            episode_id=episode.id,
            device_id="camera-x",
            area_id="area-1",
            session_id="incomplete-session",
            started_at=_now() - timedelta(seconds=10),
        ),
    )
    (root / "init.mp4").write_bytes(b"initialization")
    (root / "segments" / "segment-000000.m4s").write_bytes(b"complete-fragment")
    bundle.playlist_path.write_text(
        '#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:4.0,\nsegments/segment-000000.m4s\n',
        encoding="utf-8",
    )

    async def fail_publication(_evidence):
        raise RuntimeError("database unavailable")

    recorder = RecordingEngine(
        repo,
        bus,
        config.data_dir,
        evidence_sink=fail_publication,
    )
    restarted_recorder = None
    try:
        await recorder.start()
        await recorder.recover_interrupted_recordings()
        recording = recorder._recoverable[(episode.id, "camera-x")]

        with pytest.raises(RuntimeError, match="database unavailable"):
            await recorder._finalize_bundle(
                recording,
                incomplete=True,
                reason="episode_finalizing_during_startup",
            )

        assert recording.bundle.capture_state_path.exists()
        capture_state = json.loads(recording.bundle.capture_state_path.read_text())
        assert capture_state["incomplete"] is True
        assert capture_state["incomplete_reason"] == "episode_finalizing_during_startup"
        assert json.loads(recording.bundle.component_manifest_path.read_text())["state"] == (
            "incomplete"
        )

        await recorder.stop()

        async def persist_evidence(evidence):
            await repo.create_evidence(evidence)
            return evidence

        restarted_recorder = RecordingEngine(
            repo,
            bus,
            config.data_dir,
            evidence_sink=persist_evidence,
        )
        await restarted_recorder.start()
        await restarted_recorder.recover_interrupted_recordings()
        recovered = restarted_recorder._recoverable[(episode.id, "camera-x")]
        assert recovered.bundle.state.incomplete is True

        await restarted_recorder.finalize_episode(episode.id)

        evidence = await repo.get_evidence("evidence-incomplete")
        assert evidence is not None
        assert evidence.evidence_type == "incomplete_recording"
        assert evidence.metadata["status"] == "incomplete"
        assert evidence.metadata["playlist_validation"]["valid"] is True
        final_manifest = json.loads(recovered.bundle.component_manifest_path.read_text())
        assert final_manifest["state"] == "incomplete"
        assert not recovered.bundle.capture_state_path.exists()
    finally:
        await recorder.stop()
        if restarted_recorder is not None:
            await restarted_recorder.stop()
        await repo.close()


@pytest.mark.asyncio
async def test_persisted_active_episode_resumes_all_reconstructed_targets(repo, bus, config):
    await repo.initialize()
    await _add_areas(repo, "area-1")
    source = _video_device("camera-a", "area-1", "on_event")
    peer = _video_device("camera-b", "area-1", "on_episode")
    await repo.upsert_device(source)
    await repo.upsert_device(peer)
    now = _now()
    episode = Episode(
        id="episode-1",
        primary_area_id="area-1",
        start_time=now,
        last_event_time=now,
        last_activity_at=now,
        minimum_end_at=now + timedelta(seconds=60),
        state=EpisodeState.ACTIVE,
    )
    await repo.create_episode(episode)
    await repo.create_event(
        Event(
            device_id=source.id,
            area_id=source.area_id,
            timestamp=now,
            event_type="motion_detection",
            event_state=EventState.ACTIVE,
            source="test",
            episode_id=episode.id,
        )
    )
    recorder = RecordingEngine(repo, bus, config.data_dir)
    resumed = []

    async def start_recording(
        episode_id, device, stream_url, *, video_handler=None, codec_hint="", audio_codec_hint=""
    ):
        resumed.append((episode_id, device.id, stream_url))
        recorder._recordings[(episode_id, device.id)] = SimpleNamespace(
            episode_id=episode_id,
            device_id=device.id,
            session_id=f"session-{device.id}",
            evidence_id=f"evidence-{device.id}",
        )

    recorder._start_recording = start_recording
    await recorder.start()
    await recorder.resume_active_episodes()

    assert {(episode_id, device_id) for episode_id, device_id, _url in resumed} == {
        (episode.id, source.id),
        (episode.id, peer.id),
    }

    recorder._recordings.clear()
    await recorder.stop()
    await repo.close()


@pytest.mark.asyncio
async def test_resume_skips_recording_when_episode_closes_during_guarded_start(repo, bus, config):
    await repo.initialize()
    await _add_areas(repo, "area-1")
    device = _video_device("camera-a", "area-1", "on_episode")
    await repo.upsert_device(device)
    now = _now()
    episode = Episode(
        id="episode-resume-race",
        primary_area_id="area-1",
        start_time=now,
        last_event_time=now,
        last_activity_at=now,
        minimum_end_at=now + timedelta(seconds=60),
        state=EpisodeState.ACTIVE,
    )
    await repo.create_episode(episode)
    await repo.create_event(
        Event(
            device_id=device.id,
            area_id=device.area_id,
            timestamp=now,
            event_type="motion_detection",
            event_state=EventState.ACTIVE,
            source="test",
            episode_id=episode.id,
        )
    )
    recorder = RecordingEngine(repo, bus, config.data_dir)

    guarded_start = recorder._start_recording

    async def start_recording(*args, **kwargs):
        await repo.update_episode_state(episode.id, EpisodeState.FINALIZING)
        await guarded_start(*args, **kwargs)

    recorder._start_recording = start_recording
    await recorder.start()
    await recorder.resume_active_episodes()

    assert recorder._recordings == {}
    journal_path = Path(config.data_dir, "episodes", episode.id, "journal.ndjson")
    assert "recording.resumed" not in journal_path.read_text(encoding="utf-8")
    await recorder.stop()
    await repo.close()


@pytest.mark.asyncio
async def test_persisted_quiescent_episode_resumes_only_within_grace(repo, bus, config):
    await repo.initialize()
    await _add_areas(repo, "area-1")
    source = _video_device("camera-a", "area-1", "on_event")
    await repo.upsert_device(source)
    now = _now()
    within = Episode(
        id="episode-within-grace",
        primary_area_id="area-1",
        start_time=now - timedelta(seconds=20),
        last_event_time=now - timedelta(seconds=20),
        last_activity_at=now - timedelta(seconds=20),
        minimum_end_at=now - timedelta(seconds=2),
        state=EpisodeState.QUIESCENT,
    )
    beyond = Episode(
        id="episode-beyond-grace",
        primary_area_id="area-1",
        start_time=now - timedelta(seconds=30),
        last_event_time=now - timedelta(seconds=30),
        last_activity_at=now - timedelta(seconds=30),
        minimum_end_at=now - timedelta(seconds=10),
        state=EpisodeState.QUIESCENT,
    )
    await repo.create_episode(within)
    await repo.create_episode(beyond)
    for episode in (within, beyond):
        await repo.create_event(
            Event(
                device_id=source.id,
                area_id=source.area_id,
                timestamp=episode.last_event_time,
                event_type="motion_detection",
                event_state=EventState.ACTIVE,
                source="test",
                episode_id=episode.id,
            )
        )

    recorder = RecordingEngine(repo, bus, config.data_dir)
    resumed = []

    async def start_recording(
        episode_id, device, stream_url, *, video_handler=None, codec_hint="", audio_codec_hint=""
    ):
        resumed.append(episode_id)
        recorder._recordings[(episode_id, device.id)] = SimpleNamespace(
            episode_id=episode_id,
            device_id=device.id,
            session_id=f"session-{episode_id}",
            evidence_id=f"evidence-{episode_id}",
        )

    recorder._start_recording = start_recording
    await recorder.start()
    await recorder.resume_active_episodes()

    assert resumed == [within.id]
    recorder._recordings.clear()
    await recorder.stop()
    await repo.close()


@pytest.mark.asyncio
async def test_engine_start_closes_expired_persisted_episode_before_resume(repo, bus, config):
    await repo.initialize()
    await _add_areas(repo, "area-1")
    now = _now()
    episode = Episode(
        id="expired-episode",
        primary_area_id="area-1",
        start_time=now - timedelta(minutes=2),
        last_event_time=now - timedelta(minutes=2),
        last_activity_at=now - timedelta(minutes=2),
        minimum_end_at=now - timedelta(minutes=1),
        state=EpisodeState.ACTIVE,
    )
    await repo.create_episode(episode)
    engine = EpisodeEngine(repo, bus, timeout=config.episode_timeout)

    await engine.start()

    persisted = await repo.get_episode(episode.id)
    assert persisted.state == EpisodeState.CLOSED
    assert persisted.end_time is not None

    await engine.stop()
    await repo.close()


@pytest.mark.asyncio
async def test_failed_recording_does_not_retry_after_episode_closes(repo, bus, config, monkeypatch):
    await repo.initialize()
    await _add_areas(repo, "garagem")
    episode = Episode(
        id="episode-1",
        primary_area_id="garagem",
        state=EpisodeState.ACTIVE,
    )
    await repo.create_episode(episode)
    recorder = RecordingEngine(repo, bus, config.data_dir)
    attempts = 0
    commands = []
    published = []

    class FailedProcess:
        returncode = 1
        stderr = None

        async def wait(self):
            return self.returncode

    def failed_ffmpeg(command, **kwargs):
        nonlocal attempts
        attempts += 1
        commands.append(command)
        return FailedProcess()

    async def close_episode_during_retry(delay):
        await repo.update_episode_state(episode.id, EpisodeState.CLOSED)

    async def capture_evidence(message):
        published.append(message.data["evidence"])

    async def persisted_evidence(evidence_id):
        return published[0] if published and published[0]["id"] == evidence_id else None

    monkeypatch.setattr(recorder, "_spawn_ffmpeg", failed_ffmpeg)
    monkeypatch.setattr(asyncio, "sleep", close_episode_during_retry)
    repo.get_evidence = persisted_evidence
    bus.subscribe("evidence.received", capture_evidence)
    await recorder.start()

    await recorder._start_recording(
        episode.id,
        _video_device("cam-garagem", "garagem", "on_event"),
        "rtsp://camera-garagem/stream",
    )
    recording = recorder._recordings[(episode.id, "cam-garagem")]
    await recording.task

    assert attempts == 1
    assert commands[0][commands[0].index("-f") + 1] == "hls"
    assert commands[0][commands[0].index("-hls_time") + 1] == "4"
    assert commands[0][commands[0].index("-hls_base_url") + 1] == "segments/"
    assert commands[0][-1] == "index.m3u8"
    assert recorder._recordings == {}

    await recorder.stop()
    await repo.close()


@pytest.mark.asyncio
async def test_timed_out_recording_probe_is_reaped(repo, bus, config, monkeypatch):
    recorder = RecordingEngine(repo, bus, config.data_dir)

    class HungProcess:
        returncode = None
        killed = False
        waited = False

        async def communicate(self):
            await asyncio.Event().wait()

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            self.waited = True
            return self.returncode

    process = HungProcess()

    async def start_probe(*args, **kwargs):
        return process

    async def timeout(awaitable, timeout):
        awaitable.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start_probe)
    monkeypatch.setattr(asyncio, "wait_for", timeout)

    assert await recorder._has_video_stream("/tmp/recording.mp4") is False
    assert process.killed is True
    assert process.waited is True


@pytest.mark.asyncio
async def test_event_without_area_does_not_activate_all_cameras(repo, bus, config):
    await repo.initialize()
    await _add_areas(repo, "area-1")
    await repo.upsert_device(_video_device("camera-x", "area-1", "on_episode"))

    engine = EpisodeEngine(repo, bus, timeout=config.episode_timeout)
    recorder = RecordingEngine(repo, bus, config.data_dir)
    started, _ = _replace_recording_processes(recorder)
    await engine.start()
    await recorder.start()

    await bus.publish(
        Message(
            type="event.received",
            data={
                "event": {
                    "device_id": "unassigned-sensor",
                    "area_id": "",
                    "timestamp": _now(),
                    "event_type": "manual",
                    "event_state": EventState.ACTIVE.value,
                    "source": "test",
                }
            },
        )
    )

    assert started == []
    assert await repo.list_episodes() == []

    await recorder.stop()
    await engine.stop()
    await repo.close()
