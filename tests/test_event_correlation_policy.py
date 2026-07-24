from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from episode.config import EpisodeConfig
from episode.connectors.onvif.parser import ONVIFNotification, ONVIFStateTracker
from episode.domain.models import EventState
from episode.engine.bus import EventBus, Message
from episode.engine.engine import EpisodeEngine
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
        assert episodes[0].last_event_time == timestamp
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
