from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime, timezone

import pytest

from episode.config import EpisodeConfig
from episode.domain.models import Device, EventState
from episode.engine.bus import EventBus, Message
from episode.engine.engine import EpisodeEngine
from episode.recording.engine import RecordingEngine
from episode.storage.repository import Repository


@pytest.fixture
def config():
    tmpdir = tempfile.mkdtemp()
    return EpisodeConfig(
        data_dir=tmpdir,
        db_path=os.path.join(tmpdir, "test.db"),
        evidence_dir=os.path.join(tmpdir, "evidence"),
        episode_timeout=2,
        recording={"post_seconds": 3, "pre_seconds": 0},
    )


@pytest.fixture
def repo(config):
    return Repository(config)


@pytest.fixture
def bus():
    return EventBus()


def _now():
    return datetime.now(tz=timezone.utc)


@pytest.mark.asyncio
async def test_recording_skips_non_video_device(repo, bus, config):
    await repo.initialize()
    device = Device(
        id="device-no-video",
        name="Door Contact",
        device_type="contact",
        area_id="area-1",
        capabilities=[],
    )
    await repo.upsert_device(device)

    engine = EpisodeEngine(repo, bus, timeout=config.episode_timeout)
    recorder = RecordingEngine(repo, bus, config.evidence_dir, post_seconds=3)
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
    device = Device(
        id="device-video-no-url",
        name="Camera without config",
        device_type="hikvision",
        area_id="area-1",
        capabilities=["video"],
    )
    await repo.upsert_device(device)

    engine = EpisodeEngine(repo, bus, timeout=config.episode_timeout)
    recorder = RecordingEngine(repo, bus, config.evidence_dir, post_seconds=3)
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
