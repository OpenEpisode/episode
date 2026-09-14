from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from episode.config import EpisodeConfig
from episode.domain.models import Area, Device, EpisodeState, Event, EventState, IngestionReceipt
from episode.engine.bus import EventBus, Message
from episode.engine.engine import EpisodeEngine
from episode.plugins.onvif.events import ONVIFNotification, ONVIFStateTracker
from episode.storage.repository import Repository


def test_equivalent_onvif_motion_topics_form_one_semantic_state():
    timestamp = datetime(2026, 7, 23, 15, 0, tzinfo=timezone.utc)
    video_source = ONVIFNotification(
        topic="tns1:VideoSource/MotionAlarm",
        timestamp=timestamp,
        property_operation="Changed",
        items={"Source": "VideoSource_1", "State": "true"},
        event_type="motion_detection",
        event_state=EventState.ACTIVE,
    )
    analytics_rule = ONVIFNotification(
        topic="tns1:RuleEngine/CellMotionDetector/Motion",
        timestamp=timestamp + timedelta(seconds=1),
        property_operation="Changed",
        items={
            "VideoSourceConfigurationToken": "VideoSourceToken",
            "Rule": "MyMotionDetectorRule",
            "IsMotion": "true",
        },
        event_type="motion_detection",
        event_state=EventState.ACTIVE,
    )
    tracker = ONVIFStateTracker()

    assert tracker.is_transition(video_source) is True
    assert tracker.is_transition(analytics_rule) is False
    assert (
        tracker.is_transition(
            replace(
                video_source,
                timestamp=timestamp + timedelta(seconds=2),
                items={"Source": "VideoSource_1", "State": "false"},
                event_state=EventState.INACTIVE,
            )
        )
        is False
    )
    assert (
        tracker.is_transition(
            replace(
                analytics_rule,
                timestamp=timestamp + timedelta(seconds=3),
                items={
                    "VideoSourceConfigurationToken": "VideoSourceToken",
                    "Rule": "MyMotionDetectorRule",
                    "IsMotion": "false",
                },
                event_state=EventState.INACTIVE,
            )
        )
        is True
    )


@pytest.mark.asyncio
async def test_triggering_devices_extend_episode_with_their_own_activity_windows(tmp_path):
    config = EpisodeConfig(data_dir=str(tmp_path), db_path=str(tmp_path / "episode.db"))
    repo = Repository(config)
    engine = EpisodeEngine(repo, EventBus(), timeout=15)
    await repo.initialize()
    await repo.upsert_area(Area(id="entrance", name="Entrance"))
    await repo.upsert_device(
        Device(
            id="camera",
            name="Camera",
            device_type="camera",
            area_id="entrance",
            activity_window_seconds=30,
        )
    )
    await repo.upsert_device(
        Device(
            id="doorbell",
            name="Doorbell",
            device_type="doorbell",
            area_id="entrance",
            activity_window_seconds=90,
        )
    )
    await engine.start()
    try:
        camera = await engine.ingest_event(
            Event(
                device_id="camera",
                area_id="entrance",
                event_type="motion_detection",
            )
        )
        first = await repo.get_episode(camera.event.episode_id)
        assert 29 <= (first.minimum_end_at - first.last_activity_at).total_seconds() <= 31

        doorbell = await engine.ingest_event(
            Event(device_id="doorbell", area_id="entrance", event_type="doorbell")
        )
        extended = await repo.get_episode(doorbell.event.episode_id)
        doorbell_deadline = extended.minimum_end_at
        assert doorbell.event.episode_id == camera.event.episode_id
        assert 89 <= (doorbell_deadline - extended.last_activity_at).total_seconds() <= 91

        await engine.ingest_event(
            Event(
                device_id="camera",
                area_id="entrance",
                timestamp=datetime.now(timezone.utc) + timedelta(microseconds=1),
                event_type="human_detection",
            )
        )
        assert (await repo.get_episode(camera.event.episode_id)).minimum_end_at >= doorbell_deadline
    finally:
        await engine.stop()
        await repo.close()


@pytest.mark.asyncio
async def test_episode_created_is_emitted_once_for_the_triggering_event(tmp_path):
    config = EpisodeConfig(data_dir=str(tmp_path), db_path=str(tmp_path / "episode.db"))
    repo = Repository(config)
    bus = EventBus()
    engine = EpisodeEngine(repo, bus, timeout=30)
    await repo.initialize()
    await repo.upsert_area(Area(id="entrance", name="Entrance"))
    await repo.upsert_device(
        Device(id="camera", name="Camera", device_type="camera", area_id="entrance")
    )
    created_messages = []

    async def observe(message):
        created_messages.append(message.data)

    bus.subscribe("episode.created", observe)
    try:
        first = await engine.ingest_event(
            Event(device_id="camera", area_id="entrance", event_type="motion_detection")
        )
        await engine.ingest_event(
            Event(device_id="camera", area_id="entrance", event_type="human_detection")
        )

        assert created_messages == [
            {"episode_id": first.event.episode_id, "event_id": first.event.id}
        ]
    finally:
        await engine.stop()
        await repo.close()


async def _stored_receipt(repo, received_at: datetime) -> IngestionReceipt:
    receipt = IngestionReceipt(source="test", received_at=received_at)
    await repo.create_ingestion_receipt(receipt)
    return receipt


@pytest.mark.asyncio
async def test_engine_notifies_quiescent_then_closed_transitions(tmp_path):
    config = EpisodeConfig(data_dir=str(tmp_path), db_path=str(tmp_path / "episode.db"))
    repo = Repository(config)
    bus = EventBus()
    engine = EpisodeEngine(repo, bus, timeout=30)
    await repo.initialize()
    await repo.upsert_area(Area(id="entrance", name="Entrance"))
    await repo.upsert_device(
        Device(
            id="camera",
            name="Camera",
            device_type="camera",
            area_id="entrance",
            activity_window_seconds=1,
        )
    )
    transitions = []

    async def observe(message):
        transitions.append(message.data.get("state"))

    bus.subscribe("episode.updated", observe)
    try:
        base = datetime.now(timezone.utc) - timedelta(seconds=3)
        created = await engine.ingest_event(
            Event(
                device_id="camera",
                area_id="entrance",
                timestamp=base,
                event_type="motion_detection",
            ),
            receipt=await _stored_receipt(repo, base),
        )
        await engine._close_timed_out_episodes()
        assert (await repo.get_episode(created.event.episode_id)).state == EpisodeState.QUIESCENT
        await engine._close_timed_out_episodes(now=base + timedelta(seconds=7))
        assert (await repo.get_episode(created.event.episode_id)).state == EpisodeState.CLOSED
        assert transitions[-3:] == [
            EpisodeState.QUIESCENT.value,
            EpisodeState.FINALIZING.value,
            EpisodeState.CLOSED.value,
        ]
    finally:
        await engine.stop()
        await repo.close()


@pytest.mark.asyncio
async def test_active_event_within_quiescent_grace_reuses_episode(tmp_path):
    config = EpisodeConfig(data_dir=str(tmp_path), db_path=str(tmp_path / "episode.db"))
    repo = Repository(config)
    engine = EpisodeEngine(repo, EventBus(), timeout=30)
    await repo.initialize()
    await repo.upsert_area(Area(id="entrance", name="Entrance"))
    await repo.upsert_device(
        Device(
            id="camera",
            name="Camera",
            device_type="camera",
            area_id="entrance",
            activity_window_seconds=1,
        )
    )
    try:
        base = datetime.now(timezone.utc) - timedelta(seconds=3)
        first = await engine.ingest_event(
            Event(
                device_id="camera",
                area_id="entrance",
                timestamp=base,
                event_type="motion_detection",
            ),
            receipt=await _stored_receipt(repo, base),
        )
        episode_id = first.event.episode_id
        quiescent, closed = await repo.transition_timed_out_episodes(
            timeout=30,
            quiescent_grace_seconds=5,
            now=base + timedelta(seconds=2),
        )
        assert [episode.id for episode in quiescent] == [episode_id]
        assert closed == []

        continued = await engine.ingest_event(
            Event(
                device_id="camera",
                area_id="entrance",
                timestamp=base + timedelta(seconds=3),
                event_type="human_detection",
            ),
            receipt=await _stored_receipt(repo, base + timedelta(seconds=3)),
        )
        episode = await repo.get_episode(episode_id)
        assert continued.event.episode_id == episode_id
        assert episode.state == EpisodeState.ACTIVE
        assert episode.minimum_end_at >= base + timedelta(seconds=4)
    finally:
        await engine.stop()
        await repo.close()


@pytest.mark.asyncio
async def test_active_event_after_quiescent_grace_starts_new_episode(tmp_path):
    config = EpisodeConfig(data_dir=str(tmp_path), db_path=str(tmp_path / "episode.db"))
    repo = Repository(config)
    engine = EpisodeEngine(repo, EventBus(), timeout=30)
    await repo.initialize()
    await repo.upsert_area(Area(id="entrance", name="Entrance"))
    await repo.upsert_device(
        Device(
            id="camera",
            name="Camera",
            device_type="camera",
            area_id="entrance",
            activity_window_seconds=1,
        )
    )
    try:
        base = datetime.now(timezone.utc) - timedelta(seconds=10)
        first = await engine.ingest_event(
            Event(
                device_id="camera",
                area_id="entrance",
                timestamp=base,
                event_type="motion_detection",
            ),
            receipt=await _stored_receipt(repo, base),
        )
        first_id = first.event.episode_id
        quiescent, closed = await repo.transition_timed_out_episodes(
            timeout=30,
            quiescent_grace_seconds=5,
            now=base + timedelta(seconds=7),
        )
        assert quiescent == []
        assert [episode.id for episode in closed] == [first_id]

        second = await engine.ingest_event(
            Event(
                device_id="camera",
                area_id="entrance",
                timestamp=base + timedelta(seconds=7),
                event_type="motion_detection",
            ),
            receipt=await _stored_receipt(repo, base + timedelta(seconds=7)),
        )
        assert second.event.episode_id != first_id
        assert len(await repo.list_episodes()) == 2
    finally:
        await engine.stop()
        await repo.close()


@pytest.mark.asyncio
async def test_inactive_event_during_quiescence_attaches_without_reactivation(tmp_path):
    config = EpisodeConfig(data_dir=str(tmp_path), db_path=str(tmp_path / "episode.db"))
    repo = Repository(config)
    engine = EpisodeEngine(repo, EventBus(), timeout=30)
    await repo.initialize()
    await repo.upsert_area(Area(id="entrance", name="Entrance"))
    await repo.upsert_device(
        Device(
            id="camera",
            name="Camera",
            device_type="camera",
            area_id="entrance",
            activity_window_seconds=1,
        )
    )
    try:
        base = datetime.now(timezone.utc) - timedelta(seconds=3)
        first = await engine.ingest_event(
            Event(
                device_id="camera",
                area_id="entrance",
                timestamp=base,
                event_type="motion_detection",
            ),
            receipt=await _stored_receipt(repo, base),
        )
        episode_id = first.event.episode_id
        await repo.transition_timed_out_episodes(
            timeout=30,
            quiescent_grace_seconds=5,
            now=base + timedelta(seconds=2),
        )
        before = await repo.get_episode(episode_id)

        inactive = await engine.ingest_event(
            Event(
                device_id="camera",
                area_id="entrance",
                timestamp=base + timedelta(seconds=3),
                event_type="motion_detection",
                event_state=EventState.INACTIVE,
            ),
            receipt=await _stored_receipt(repo, base + timedelta(seconds=3)),
        )
        after = await repo.get_episode(episode_id)
        assert inactive.event.episode_id == episode_id
        assert after.state == EpisodeState.QUIESCENT
        assert after.minimum_end_at == before.minimum_end_at
    finally:
        await engine.stop()
        await repo.close()


@pytest.mark.asyncio
async def test_ingress_time_keeps_event_in_episode_when_processing_is_delayed(
    tmp_path, monkeypatch
):
    config = EpisodeConfig(data_dir=str(tmp_path), db_path=str(tmp_path / "episode.db"))
    repo = Repository(config)
    engine = EpisodeEngine(repo, EventBus(), timeout=30, quiescent_grace_seconds=0)
    await repo.initialize()
    await repo.upsert_area(Area(id="entrance", name="Entrance"))
    await repo.upsert_device(
        Device(
            id="camera",
            name="Camera",
            device_type="camera",
            area_id="entrance",
            activity_window_seconds=2,
        )
    )
    original_canonicalize = repo.canonicalize_event

    async def delayed_canonicalize(candidate):
        if candidate.event_type == "human_detection":
            await asyncio.sleep(2)
        return await original_canonicalize(candidate)

    monkeypatch.setattr(repo, "canonicalize_event", delayed_canonicalize)
    try:
        base = datetime.now(timezone.utc) - timedelta(seconds=1)
        first = await engine.ingest_event(
            Event(
                device_id="camera",
                area_id="entrance",
                timestamp=base,
                event_type="motion_detection",
            ),
            receipt=await _stored_receipt(repo, base),
        )
        delayed = await engine.ingest_event(
            Event(
                device_id="camera",
                area_id="entrance",
                timestamp=base + timedelta(milliseconds=500),
                event_type="human_detection",
            ),
            receipt=await _stored_receipt(repo, base + timedelta(milliseconds=500)),
        )
        assert delayed.event.episode_id == first.event.episode_id
    finally:
        await engine.stop()
        await repo.close()


@pytest.mark.asyncio
async def test_inactive_event_attaches_without_extending_episode(tmp_path):
    config = EpisodeConfig(
        data_dir=str(tmp_path),
        db_path=str(tmp_path / "episode.db"),
        episode_timeout=30,
    )
    repo = Repository(config)
    bus = EventBus()
    engine = EpisodeEngine(repo, bus, timeout=30)
    await repo.initialize()
    await repo.upsert_area(Area(id="entrance", name="Entrance"))
    await repo.upsert_device(
        Device(id="camera-1", name="Camera 1", device_type="camera", area_id="entrance")
    )
    await engine.start()
    try:
        timestamp = datetime.now(timezone.utc)
        common = {
            "device_id": "camera-1",
            "area_id": "entrance",
            "event_type": "motion_detection",
            "source": "onvif:events",
        }
        await bus.publish(
            Message(
                type="event.received",
                data={
                    "event": {
                        **common,
                        "timestamp": timestamp,
                        "event_state": EventState.ACTIVE.value,
                    }
                },
            )
        )
        activity_before = (await repo.list_episodes())[0].last_activity_at
        await bus.publish(
            Message(
                type="event.received",
                data={
                    "event": {
                        **common,
                        "timestamp": timestamp + timedelta(seconds=10),
                        "event_state": EventState.INACTIVE.value,
                    }
                },
            )
        )

        episodes = await repo.list_episodes()
        events = await repo.list_events(episode_id=episodes[0].id)
        assert len(episodes) == 1
        assert episodes[0].event_count == 2
        assert episodes[0].last_event_time == timestamp + timedelta(seconds=10)
        assert episodes[0].last_activity_at == activity_before
        assert {event.event_state for event in events} == {
            EventState.ACTIVE,
            EventState.INACTIVE,
        }
    finally:
        await engine.stop()
        await repo.close()


@pytest.mark.asyncio
async def test_inactive_event_does_not_open_episode(tmp_path):
    config = EpisodeConfig(
        data_dir=str(tmp_path),
        db_path=str(tmp_path / "episode.db"),
        episode_timeout=30,
    )
    repo = Repository(config)
    bus = EventBus()
    engine = EpisodeEngine(repo, bus, timeout=30)
    await repo.initialize()
    await repo.upsert_area(Area(id="entrance", name="Entrance"))
    await repo.upsert_device(
        Device(id="camera-1", name="Camera 1", device_type="camera", area_id="entrance")
    )
    await engine.start()
    try:
        await bus.publish(
            Message(
                type="event.received",
                data={
                    "event": {
                        "device_id": "camera-1",
                        "area_id": "entrance",
                        "timestamp": datetime.now(timezone.utc),
                        "event_type": "motion_detection",
                        "event_state": EventState.INACTIVE.value,
                        "source": "onvif:events",
                    }
                },
            )
        )

        assert await repo.list_episodes() == []
        events = await repo.list_events()
        assert len(events) == 1
        assert events[0].episode_id is None
    finally:
        await engine.stop()
        await repo.close()


@pytest.mark.asyncio
async def test_late_inactive_stays_unassigned_after_episode_closes(tmp_path):
    config = EpisodeConfig(
        data_dir=str(tmp_path),
        db_path=str(tmp_path / "episode.db"),
        episode_timeout=30,
    )
    repo = Repository(config)
    bus = EventBus()
    engine = EpisodeEngine(repo, bus, timeout=30)
    await repo.initialize()
    await repo.upsert_area(Area(id="entrance", name="Entrance"))
    await repo.upsert_device(
        Device(id="doorbell", name="Doorbell", device_type="doorbell", area_id="entrance")
    )
    await engine.start()
    try:
        timestamp = datetime.now(timezone.utc)
        active = await engine.ingest_event(
            Event(
                device_id="doorbell",
                area_id="entrance",
                timestamp=timestamp,
                event_type="doorbell",
                event_state=EventState.ACTIVE,
                source="test",
            )
        )
        episode_id = active.event.episode_id
        await repo.update_episode_state(episode_id, EpisodeState.CLOSED)
        closed_before = await repo.get_episode(episode_id)

        inactive_timestamp = timestamp + timedelta(seconds=95)
        inactive = await engine.ingest_event(
            Event(
                device_id="doorbell",
                area_id="entrance",
                timestamp=inactive_timestamp,
                event_type="doorbell",
                event_state=EventState.INACTIVE,
                source="test",
            )
        )

        closed_after = await repo.get_episode(episode_id)
        assert inactive.event.episode_id is None
        assert closed_after.state == EpisodeState.CLOSED
        assert closed_after.end_time == closed_before.end_time
        assert closed_after.last_activity_at == closed_before.last_activity_at
        assert closed_after.last_event_time == closed_before.last_event_time
        assert closed_after.event_count == 1
    finally:
        await engine.stop()
        await repo.close()


@pytest.mark.asyncio
async def test_inactive_transition_is_not_attached_twice(tmp_path):
    config = EpisodeConfig(data_dir=str(tmp_path), db_path=str(tmp_path / "episode.db"))
    repo = Repository(config)
    engine = EpisodeEngine(repo, EventBus(), timeout=30)
    await repo.initialize()
    await repo.upsert_area(Area(id="entrance", name="Entrance"))
    await repo.upsert_device(
        Device(id="doorbell", name="Doorbell", device_type="doorbell", area_id="entrance")
    )
    await engine.start()
    try:
        timestamp = datetime.now(timezone.utc)
        active = await engine.ingest_event(
            Event(
                device_id="doorbell",
                area_id="entrance",
                timestamp=timestamp,
                event_type="doorbell",
                event_state=EventState.ACTIVE,
                source="test",
            )
        )
        first_inactive = await engine.ingest_event(
            Event(
                device_id="doorbell",
                area_id="entrance",
                timestamp=timestamp + timedelta(seconds=60),
                event_type="doorbell",
                event_state=EventState.INACTIVE,
                source="test",
            )
        )
        second_inactive = await engine.ingest_event(
            Event(
                device_id="doorbell",
                area_id="entrance",
                timestamp=timestamp + timedelta(seconds=61),
                event_type="doorbell",
                event_state=EventState.INACTIVE,
                source="test",
            )
        )

        assert first_inactive.event.episode_id == active.event.episode_id
        assert second_inactive.event.episode_id is None
        assert (await repo.get_episode(active.event.episode_id)).event_count == 2
    finally:
        await engine.stop()
        await repo.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("device_id", "event_type"),
    [("other-doorbell", "doorbell"), ("doorbell", "motion_detection")],
)
async def test_inactive_transition_does_not_cross_semantic_streams(tmp_path, device_id, event_type):
    config = EpisodeConfig(data_dir=str(tmp_path), db_path=str(tmp_path / "episode.db"))
    repo = Repository(config)
    engine = EpisodeEngine(repo, EventBus(), timeout=30)
    await repo.initialize()
    await repo.upsert_area(Area(id="entrance", name="Entrance"))
    await repo.upsert_device(
        Device(id="doorbell", name="Doorbell", device_type="doorbell", area_id="entrance")
    )
    await repo.upsert_device(
        Device(
            id="other-doorbell",
            name="Other Doorbell",
            device_type="doorbell",
            area_id="entrance",
        )
    )
    await engine.start()
    try:
        timestamp = datetime.now(timezone.utc)
        await engine.ingest_event(
            Event(
                device_id="doorbell",
                area_id="entrance",
                timestamp=timestamp,
                event_type="doorbell",
                event_state=EventState.ACTIVE,
                source="test",
            )
        )
        inactive = await engine.ingest_event(
            Event(
                device_id=device_id,
                area_id="entrance",
                timestamp=timestamp + timedelta(seconds=60),
                event_type=event_type,
                event_state=EventState.INACTIVE,
                source="test",
            )
        )

        assert inactive.event.episode_id is None
    finally:
        await engine.stop()
        await repo.close()
