from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from episode.config import EpisodeConfig
from episode.domain.models import EpisodeState, Event, EventState
from episode.engine.bus import EventBus, Message
from episode.engine.engine import EpisodeEngine
from episode.storage import repository as repository_module
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
async def test_stale_device_timestamps_do_not_expire_active_episode(engine, repo, bus):
    stale = datetime.now(tz=timezone.utc) - timedelta(hours=1)
    common = {
        "device_id": "device-1",
        "area_id": "area-1",
        "event_type": "human_detection",
        "event_state": EventState.ACTIVE.value,
        "source": "hikvision:alarm_server",
    }

    await bus.publish(
        Message(
            type="event.received",
            data={"event": {**common, "timestamp": stale}},
        )
    )

    assert await repo.close_timed_out_episodes(timeout=2) == []

    await bus.publish(
        Message(
            type="event.received",
            data={"event": {**common, "timestamp": stale + timedelta(seconds=1)}},
        )
    )

    episodes = await repo.list_episodes()
    assert len(episodes) == 1
    assert episodes[0].event_count == 2
    assert episodes[0].state == EpisodeState.ACTIVE
    assert episodes[0].last_event_time == stale + timedelta(seconds=1)
    assert episodes[0].last_activity_at > stale + timedelta(minutes=59)


@pytest.mark.asyncio
async def test_slow_event_processing_refreshes_activity_at_completion(
    engine,
    repo,
    bus,
    monkeypatch,
):
    original_add = repo.add_event_to_episode

    async def delayed_add(*args, **kwargs):
        await asyncio.sleep(2.3)
        return await original_add(*args, **kwargs)

    monkeypatch.setattr(repo, "add_event_to_episode", delayed_add)
    event_time = datetime.now(tz=timezone.utc) - timedelta(hours=1)

    await bus.publish(
        Message(
            type="event.received",
            data={
                "event": {
                    "device_id": "device-1",
                    "area_id": "area-1",
                    "timestamp": event_time,
                    "event_type": "human_detection",
                    "event_state": EventState.ACTIVE.value,
                    "source": "hikvision:alarm_server",
                }
            },
        )
    )

    completed_at = datetime.now(tz=timezone.utc)
    episodes = await repo.list_episodes()
    assert len(episodes) == 1
    assert episodes[0].last_activity_at >= completed_at - timedelta(seconds=0.5)
    assert await repo.close_timed_out_episodes(timeout=2) == []


@pytest.mark.asyncio
async def test_concurrent_manifest_refresh_cannot_overwrite_newer_state(
    engine,
    repo,
    bus,
    monkeypatch,
):
    timestamp = datetime.now(tz=timezone.utc)
    await bus.publish(
        Message(
            type="event.received",
            data={
                "event": {
                    "device_id": "device-1",
                    "area_id": "area-1",
                    "timestamp": timestamp,
                    "event_type": "human_detection",
                    "event_state": EventState.ACTIVE.value,
                    "source": "test",
                }
            },
        )
    )
    episode = (await repo.list_episodes())[0]
    initial_event = (await repo.list_events(episode_id=episode.id))[0]

    first_write_started = threading.Event()
    release_first_write = threading.Event()
    write_count = 0
    count_lock = threading.Lock()
    original_write = repository_module.write_manifest

    def delayed_first_write(*args, **kwargs):
        nonlocal write_count
        with count_lock:
            write_count += 1
            is_first = write_count == 1
        if is_first:
            first_write_started.set()
            assert release_first_write.wait(timeout=5)
        return original_write(*args, **kwargs)

    monkeypatch.setattr(repository_module, "write_manifest", delayed_first_write)
    stale_refresh = asyncio.create_task(repo.refresh_episode_manifest(episode.id))
    assert await asyncio.to_thread(first_write_started.wait, 2)

    second_event = Event(
        device_id="device-2",
        area_id="area-1",
        timestamp=timestamp + timedelta(seconds=1),
        event_type="vehicle_detection",
        event_state=EventState.ACTIVE,
        source="test",
    )
    await repo.create_event(second_event)
    await repo.add_event_to_episode(
        second_event.id,
        episode.id,
        _defer_manifest=True,
    )
    fresh_refresh = asyncio.create_task(repo.refresh_episode_manifest(episode.id))
    await asyncio.sleep(0.1)

    release_first_write.set()
    await asyncio.gather(stale_refresh, fresh_refresh)

    manifest_path = os.path.join(
        repo._data_dir,
        "episodes",
        episode.id,
        "manifest.json",
    )
    with open(manifest_path, encoding="utf-8") as stream:
        manifest = json.load(stream)
    assert {item["id"] for item in manifest["events"]} == {
        initial_event.id,
        second_event.id,
    }


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

    await asyncio.sleep(3.0)

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
