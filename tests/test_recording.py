from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from episode.config import EpisodeConfig
from episode.domain.models import Area, CapabilityConfig, Device, Episode, EpisodeState, EventState
from episode.engine.bus import EventBus, Message
from episode.engine.engine import EpisodeEngine
from episode.recording.engine import RecordingEngine
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
        evidence_dir=os.path.join(tmpdir, "evidence"),
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
    recorder = RecordingEngine(repo, bus, config.evidence_dir)
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
    recorder = RecordingEngine(repo, bus, config.evidence_dir)
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
    recorder = RecordingEngine(repo, bus, config.evidence_dir)
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
    recorder = RecordingEngine(repo, bus, config.evidence_dir)
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
    recorder = RecordingEngine(repo, bus, config.evidence_dir)
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
    assert all(path.endswith(".mp4") for path in output_paths)
    assert all(path.endswith(".mp4.part") for path in working_paths)
    assert any("rec_cam-garagem_" in path for path in output_paths)
    assert any("rec_cam-garagem-interior_" in path for path in output_paths)

    release.set()
    await asyncio.gather(*(recording.task for recording in recordings))
    await recorder.stop()


@pytest.mark.asyncio
async def test_completed_segments_are_published_while_latest_remains_active(
    repo, bus, config, monkeypatch
):
    recorder = RecordingEngine(repo, bus, config.evidence_dir, segment_seconds=60)
    release = asyncio.Event()
    published = []

    async def hold_recording(recording, rtsp_url):
        await release.wait()

    async def capture_evidence(msg):
        published.append(msg.data["evidence"])

    async def valid_video(path):
        return True

    recorder._record_episode = hold_recording
    monkeypatch.setattr(recorder, "_has_video_stream", valid_video)
    bus.subscribe("evidence.received", capture_evidence)

    await recorder._start_recording(
        "episode-1",
        _video_device("camera-x", "area-1", "on_episode"),
        "rtsp://camera-x/stream",
    )
    recording = recorder._recordings[("episode-1", "camera-x")]
    first_working = recording.working_path.replace("%06d", "000000")
    second_working = recording.working_path.replace("%06d", "000001")
    os.makedirs(os.path.dirname(first_working), exist_ok=True)
    with open(first_working, "wb") as f:
        f.write(b"0" * 4096)
    with open(second_working, "wb") as f:
        f.write(b"1" * 4096)

    await recorder._finalize_ready_segments(recording, include_latest=False)

    first_output = first_working.removesuffix(".part")
    second_output = second_working.removesuffix(".part")
    assert os.path.exists(first_output)
    assert not os.path.exists(first_working)
    assert os.path.exists(second_working)
    assert not os.path.exists(second_output)
    assert len(published) == 1
    assert published[0]["file_path"] == first_output
    assert published[0]["metadata"]["segment_index"] == 0
    assert published[0]["metadata"]["segment_seconds"] == 60

    await recorder._finalize_ready_segments(recording, include_latest=True)

    assert os.path.exists(second_output)
    assert not os.path.exists(second_working)
    assert [item["metadata"]["segment_index"] for item in published] == [0, 1]
    assert len({item["metadata"]["recording_session_id"] for item in published}) == 1

    release.set()
    await recording.task
    await recorder.stop()


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
    recorder = RecordingEngine(repo, bus, config.evidence_dir)
    attempts = 0
    commands = []

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

    monkeypatch.setattr(asyncio, "create_subprocess_exec", failed_ffmpeg)
    monkeypatch.setattr(asyncio, "sleep", close_episode_during_retry)
    await recorder.start()

    await recorder._start_recording(
        episode.id,
        _video_device("cam-garagem", "garagem", "on_event"),
        "rtsp://camera-garagem/stream",
    )
    recording = recorder._recordings[(episode.id, "cam-garagem")]
    await recording.task

    assert attempts == 1
    assert commands[0][commands[0].index("-f") + 1] == "segment"
    assert commands[0][commands[0].index("-segment_time") + 1] == "600"
    assert commands[0][-1].endswith("_%06d.mp4.part")
    assert recorder._recordings == {}

    await recorder.stop()
    await repo.close()


@pytest.mark.asyncio
async def test_timed_out_recording_probe_is_reaped(repo, bus, config, monkeypatch):
    recorder = RecordingEngine(repo, bus, config.evidence_dir)

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
    recorder = RecordingEngine(repo, bus, config.evidence_dir)
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
