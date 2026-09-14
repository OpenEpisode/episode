from __future__ import annotations

import asyncio
import json
import os
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
from episode.engine.engine import EpisodeEngine
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

    async def start_recording(episode_id, device, rtsp_url):
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


@pytest.mark.asyncio
async def test_hls_fragments_remain_one_evidence_bundle(repo, bus, config):
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


@pytest.mark.asyncio
async def test_recorder_diagnostics_report_progress_without_stream_credentials(repo, bus, config):
    await repo.initialize()
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
    assert "rtsp" not in str(diagnostic)
    assert "private-password" not in str(status)

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

        def terminate(self):
            self.returncode = -15
            terminated.set()

        def kill(self):
            self.returncode = -9
            terminated.set()

        async def wait(self):
            await terminated.wait()
            return self.returncode

    async def start_ffmpeg(*args, **kwargs):
        return StalledProcess()

    async def persist_evidence(message):
        await repo.create_evidence(Evidence(**message.data["evidence"]))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start_ffmpeg)
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


@pytest.mark.asyncio
async def test_application_shutdown_leaves_active_hls_bundle_recoverable(
    repo, bus, config, monkeypatch
):
    recorder = RecordingEngine(repo, bus, config.data_dir, fragment_seconds=60)
    process_started = asyncio.Event()
    process_finished = asyncio.Event()
    published = []

    class ActiveProcess:
        returncode = None

        def terminate(self):
            self.returncode = 0
            process_finished.set()

        def kill(self):
            self.returncode = -9
            process_finished.set()

        async def wait(self):
            await process_finished.wait()
            return self.returncode

    async def start_ffmpeg(*args, **kwargs):
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

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start_ffmpeg)
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


@pytest.mark.asyncio
async def test_multicamera_shutdown_signals_all_processes_before_waiting(
    repo, bus, config, monkeypatch
):
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

    async def start_ffmpeg(*args, **kwargs):
        device_id = f"camera-{'a' if not processes else 'b'}"
        process = ActiveProcess(device_id)
        processes[device_id] = process
        return process

    async def capture_evidence(message):
        published.append(message.data["evidence"])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start_ffmpeg)
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

    async def start_recording(episode_id, device, stream_url):
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

    async def start_recording(episode_id, device, stream_url):
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

        async def wait(self):
            return self.returncode

    async def failed_ffmpeg(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        commands.append(args)
        return FailedProcess()

    async def close_episode_during_retry(delay):
        await repo.update_episode_state(episode.id, EpisodeState.CLOSED)

    async def capture_evidence(message):
        published.append(message.data["evidence"])

    async def persisted_evidence(evidence_id):
        return published[0] if published and published[0]["id"] == evidence_id else None

    monkeypatch.setattr(asyncio, "create_subprocess_exec", failed_ffmpeg)
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
