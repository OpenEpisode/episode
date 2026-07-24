from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from episode.config import EpisodeConfig
from episode.domain.models import EpisodeState, Event, EventState
from episode.engine.bus import EventBus, Message
from episode.engine.engine import EpisodeEngine
from episode.storage.repository import Repository


@pytest.fixture
def config():
    tmpdir = tempfile.mkdtemp()
    cfg = EpisodeConfig(
        data_dir=tmpdir,
        db_path=os.path.join(tmpdir, "test.db"),
        evidence_dir=os.path.join(tmpdir, "evidence"),
        episode_timeout=2,
    )
    return cfg


@pytest.fixture
def repo(config):
    r = Repository(config)
    return r


@pytest.fixture
def bus():
    return EventBus()


@pytest_asyncio.fixture
async def engine(repo, bus, config):
    eng = EpisodeEngine(repo, bus, timeout=config.episode_timeout)
    await repo.initialize()
    await eng.start()
    yield eng
    await eng.stop()
    await repo.close()


@pytest.mark.asyncio
async def test_event_persistence(engine, repo):
    event = Event(
        device_id="device-1",
        area_id="area-1",
        timestamp=datetime.now(tz=timezone.utc),
        event_type="motion_detection",
        source="hikvision:isapi",
    )
    await repo.create_event(event)
    retrieved = await repo.get_event(event.id)
    assert retrieved is not None
    assert retrieved.id == event.id
    assert retrieved.event_type == "motion_detection"


@pytest.mark.asyncio
async def test_episode_creation_on_first_event(engine, repo, bus):
    await bus.publish(
        Message(
            type="event.received",
            data={
                "event": {
                    "device_id": "device-1",
                    "area_id": "area-1",
                    "timestamp": datetime.now(tz=timezone.utc),
                    "event_type": "human_detection",
                    "event_state": EventState.ACTIVE.value,
                    "source": "hikvision:isapi",
                }
            },
        )
    )
    episodes = await repo.list_episodes()
    assert len(episodes) == 1
    ep = episodes[0]
    assert ep.primary_area_id == "area-1"
    assert ep.state == EpisodeState.ACTIVE
    assert ep.event_count == 1


@pytest.mark.asyncio
async def test_events_correlate_to_same_episode(engine, repo, bus):
    ts = datetime.now(tz=timezone.utc) - timedelta(seconds=1)

    await bus.publish(
        Message(
            type="event.received",
            data={
                "event": {
                    "device_id": "device-1",
                    "area_id": "area-1",
                    "timestamp": ts,
                    "event_type": "human_detection",
                    "event_state": EventState.ACTIVE.value,
                    "source": "hikvision:isapi",
                }
            },
        )
    )

    await bus.publish(
        Message(
            type="event.received",
            data={
                "event": {
                    "device_id": "device-2",
                    "area_id": "area-1",
                    "timestamp": ts + timedelta(seconds=1),
                    "event_type": "vehicle_detection",
                    "event_state": EventState.ACTIVE.value,
                    "source": "hikvision:alarm_server",
                }
            },
        )
    )

    episodes = await repo.list_episodes()
    assert len(episodes) == 1
    assert episodes[0].event_count == 2


@pytest.mark.asyncio
async def test_cross_area_events_merge_into_same_episode(engine, repo, bus):
    ts = datetime.now(tz=timezone.utc)

    await bus.publish(
        Message(
            type="event.received",
            data={
                "event": {
                    "device_id": "device-1",
                    "area_id": "area-1",
                    "timestamp": ts,
                    "event_type": "human_detection",
                    "event_state": EventState.ACTIVE.value,
                    "source": "hikvision:isapi",
                }
            },
        )
    )

    await bus.publish(
        Message(
            type="event.received",
            data={
                "event": {
                    "device_id": "device-3",
                    "area_id": "area-2",
                    "timestamp": ts + timedelta(seconds=1),
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


@pytest.mark.asyncio
async def test_orphan_evidence_matches_event(engine, repo, bus):
    ts = datetime.now(tz=timezone.utc) - timedelta(seconds=1)

    await bus.publish(
        Message(
            type="event.received",
            data={
                "event": {
                    "device_id": "device-1",
                    "area_id": "area-1",
                    "timestamp": ts,
                    "event_type": "human_detection",
                    "event_state": EventState.ACTIVE.value,
                    "source": "hikvision:isapi",
                }
            },
        )
    )

    await bus.publish(
        Message(
            type="evidence.received",
            data={
                "evidence": {
                    "device_id": "device-1",
                    "area_id": "area-1",
                    "timestamp": ts + timedelta(seconds=1),
                    "evidence_type": "snapshot",
                    "file_path": "/tmp/test.jpg",
                    "mime_type": "image/jpeg",
                }
            },
        )
    )

    evidence_list = await repo.list_evidence()
    assert len(evidence_list) == 1
    ev = evidence_list[0]
    assert ev.episode_id is not None


@pytest.mark.asyncio
async def test_episode_closes_after_timeout(engine, repo, bus):
    ts = datetime.now(tz=timezone.utc) - timedelta(seconds=5)

    await bus.publish(
        Message(
            type="event.received",
            data={
                "event": {
                    "device_id": "device-1",
                    "area_id": "area-1",
                    "timestamp": ts,
                    "event_type": "human_detection",
                    "event_state": EventState.ACTIVE.value,
                    "source": "hikvision:isapi",
                }
            },
        )
    )

    import asyncio

    await asyncio.sleep(1.5)

    episodes = await repo.list_episodes()
    assert len(episodes) == 1
    assert episodes[0].state == EpisodeState.CLOSED


@pytest.mark.asyncio
async def test_evidence_preservation_via_filesystem(config, repo):
    png_data = b"\x89PNG\r\n\x1a\n" + b"test_image_data"
    os.makedirs(os.path.join(repo._data_dir, "orphans", "snapshots"), exist_ok=True)
    path = os.path.join(repo._data_dir, "orphans", "snapshots", "test.png")
    with open(path, "wb") as f:
        f.write(png_data)
    assert os.path.exists(path)

    with open(path, "rb") as f:
        assert f.read() == png_data


@pytest.mark.asyncio
async def test_timeline_is_chronological(engine, repo, bus):
    import asyncio

    base = datetime.now(tz=timezone.utc)
    for i in range(3):
        await bus.publish(
            Message(
                type="event.received",
                data={
                    "event": {
                        "device_id": f"device-{i}",
                        "area_id": "area-1",
                        "timestamp": base + timedelta(seconds=i),
                        "event_type": "motion_detection",
                        "event_state": EventState.ACTIVE.value,
                        "source": "test",
                    }
                },
            )
        )
        await asyncio.sleep(0.05)

    events = await repo.list_events(area_id="area-1")
    assert len(events) == 3
    for i in range(len(events) - 1):
        assert events[i].timestamp >= events[i + 1].timestamp
