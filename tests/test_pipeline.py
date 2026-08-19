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
from episode.domain.models import (
    Area,
    Device,
    EpisodeState,
    Event,
    EventState,
    Evidence,
    IngestionReceipt,
)
from episode.engine.bus import EventBus, Message
from episode.engine.engine import EpisodeEngine
from episode.storage import projection as projection_module
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
    for area_id in ("area-1", "area-2"):
        await repo.upsert_area(Area(id=area_id, name=area_id))
    for device_id, area_id in (
        ("device-0", "area-1"),
        ("device-1", "area-1"),
        ("device-2", "area-1"),
        ("device-3", "area-2"),
    ):
        await repo.upsert_device(
            Device(id=device_id, name=device_id, device_type="camera", area_id=area_id)
        )
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
    original_write = projection_module.write_manifest

    def delayed_first_write(*args, **kwargs):
        nonlocal write_count
        with count_lock:
            write_count += 1
            is_first = write_count == 1
        if is_first:
            first_write_started.set()
            assert release_first_write.wait(timeout=5)
        return original_write(*args, **kwargs)

    monkeypatch.setattr(projection_module, "write_manifest", delayed_first_write)
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
async def test_cross_area_events_create_separate_episodes(engine, repo, bus):
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
    assert len(episodes) == 2
    assert {episode.primary_area_id for episode in episodes} == {"area-1", "area-2"}
    assert all(episode.event_count == 1 for episode in episodes)


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
async def test_delayed_evidence_uses_capture_time_to_join_closed_episode(
    engine,
    repo,
    bus,
    config,
):
    event_time = datetime.now(tz=timezone.utc) - timedelta(seconds=2)
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
                    "source": "hikvision:isapi",
                }
            },
        )
    )
    episode = (await repo.list_episodes())[0]
    await repo.update_episode_state(episode.id, EpisodeState.CLOSED)

    source_path = os.path.join(config.data_dir, "delayed-snapshot.jpg")
    with open(source_path, "wb") as snapshot:
        snapshot.write(b"delayed snapshot")
    observed_at = event_time + timedelta(seconds=1)
    receipt = IngestionReceipt(
        source="ftp:upload",
        received_at=datetime.now(tz=timezone.utc),
        observed_at=observed_at,
        device_id="device-1",
        area_id="area-1",
    )
    await repo.create_ingestion_receipt(receipt)

    evidence = await engine.ingest_evidence(
        Evidence(
            device_id="device-1",
            area_id="area-1",
            timestamp=observed_at,
            evidence_type="snapshot",
            file_path=source_path,
            mime_type="image/jpeg",
        ),
        receipt=receipt,
    )

    stored_receipt = await repo.get_ingestion_receipt(receipt.id)
    stored_evidence = await repo.get_evidence(evidence.id)
    updated_episode = await repo.get_episode(episode.id)
    assert evidence.episode_id == episode.id
    assert stored_receipt.episode_id == episode.id
    assert stored_receipt.evidence_id == evidence.id
    assert updated_episode.state == EpisodeState.CLOSED
    assert updated_episode.evidence_count == 1
    assert stored_evidence.file_path.startswith(
        os.path.join(config.data_dir, "episodes", episode.id, "snapshots")
    )


@pytest.mark.asyncio
async def test_delayed_evidence_prefers_containing_closed_episode_over_new_open_episode(
    engine,
    repo,
    bus,
    config,
):
    first_event_time = datetime.now(tz=timezone.utc) - timedelta(seconds=10)
    event_template = {
        "device_id": "device-1",
        "area_id": "area-1",
        "event_type": "human_detection",
        "event_state": EventState.ACTIVE.value,
        "source": "hikvision:isapi",
    }
    await bus.publish(
        Message(
            type="event.received",
            data={"event": {**event_template, "timestamp": first_event_time}},
        )
    )
    first_episode = (await repo.list_episodes())[0]
    await repo.update_episode_state(first_episode.id, EpisodeState.CLOSED)

    await bus.publish(
        Message(
            type="event.received",
            data={
                "event": {
                    **event_template,
                    "timestamp": datetime.now(tz=timezone.utc),
                }
            },
        )
    )
    episodes = await repo.list_episodes()
    open_episode = next(item for item in episodes if item.state == EpisodeState.ACTIVE)

    source_path = os.path.join(config.data_dir, "queued-snapshot.jpg")
    with open(source_path, "wb") as snapshot:
        snapshot.write(b"queued snapshot")
    evidence = await engine.ingest_evidence(
        Evidence(
            device_id="device-1",
            area_id="area-1",
            timestamp=first_event_time + timedelta(seconds=1),
            evidence_type="snapshot",
            file_path=source_path,
            mime_type="image/jpeg",
        )
    )

    assert evidence.episode_id == first_episode.id
    assert (await repo.get_episode(first_episode.id)).evidence_count == 1
    assert (await repo.get_episode(open_episode.id)).evidence_count == 0


@pytest.mark.asyncio
async def test_preset_episode_evidence_uses_the_complete_linking_path(engine, repo, bus, config):
    timestamp = datetime.now(tz=timezone.utc)
    await bus.publish(
        Message(
            type="event.received",
            data={
                "event": {
                    "device_id": "device-1",
                    "area_id": "area-1",
                    "timestamp": timestamp,
                    "event_type": "motion_detection",
                    "event_state": EventState.ACTIVE.value,
                    "source": "test",
                }
            },
        )
    )
    episode = (await repo.list_episodes())[0]
    source_path = os.path.join(config.data_dir, "recording.mp4")
    with open(source_path, "wb") as recording:
        recording.write(b"recording bytes")

    await bus.publish(
        Message(
            type="evidence.received",
            data={
                "evidence": {
                    "device_id": "device-1",
                    "area_id": "area-1",
                    "timestamp": timestamp,
                    "evidence_type": "recording",
                    "file_path": source_path,
                    "mime_type": "video/mp4",
                    "episode_id": episode.id,
                }
            },
        )
    )

    updated = await repo.get_episode(episode.id)
    evidence = await repo.list_evidence(episode_id=episode.id)
    assert updated.evidence_count == len(evidence) == 1
    assert evidence[0].episode_id == episode.id
    assert evidence[0].file_path.startswith(
        os.path.join(config.data_dir, "episodes", episode.id, "recordings")
    )
    journal = os.path.join(config.data_dir, "episodes", episode.id, "journal.ndjson")
    with open(journal, encoding="utf-8") as journal_file:
        journal_types = [json.loads(line)["type"] for line in journal_file]
    assert "evidence.added" in journal_types


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

    async with asyncio.timeout(5):
        while True:
            episodes = await repo.list_episodes()
            if episodes[0].state == EpisodeState.CLOSED:
                break
            await asyncio.sleep(0.05)

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


@pytest.mark.asyncio
async def test_orphan_evidence_does_not_cross_area_boundary(engine, repo, bus):
    timestamp = datetime.now(tz=timezone.utc)
    await bus.publish(
        Message(
            type="event.received",
            data={
                "event": {
                    "device_id": "device-1",
                    "area_id": "area-1",
                    "timestamp": timestamp,
                    "event_type": "motion_detection",
                    "event_state": EventState.ACTIVE.value,
                    "source": "test",
                }
            },
        )
    )

    await bus.publish(
        Message(
            type="evidence.received",
            data={
                "evidence": {
                    "device_id": "device-3",
                    "area_id": "area-2",
                    "timestamp": timestamp,
                    "evidence_type": "snapshot",
                    "file_path": "/tmp/area-2.jpg",
                    "mime_type": "image/jpeg",
                }
            },
        )
    )

    evidence = await repo.list_evidence()
    assert len(evidence) == 1
    assert evidence[0].episode_id is None
