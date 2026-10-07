from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from episode.actions.snapshot import SnapshotEngine
from episode.domain.models import Event, EventState
from episode.engine.bus import EventBus, Message
from episode.engine.engine import CanonicalEventResult
from episode.media import CameraMedia, MediaRegistry


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "expected_snapshots"),
    (("doorbell", 1), ("motion_detection", 0)),
)
async def test_default_snapshot_policy_captures_doorbell_events_only(
    tmp_path,
    monkeypatch,
    event_type,
    expected_snapshots,
):
    async def run_inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", run_inline)
    bus = EventBus()
    media = MediaRegistry()
    expected_bytes = b"\xff\xd8\xff\xe0doorbell snapshot\xff\xd9"
    fetches = 0
    evidence_messages: list[Message] = []

    async def fetch_snapshot():
        nonlocal fetches
        fetches += 1
        return expected_bytes, "image/jpeg"

    media.register(
        CameraMedia(
            device_id="doorbell",
            source="hikvision-sdk-snapshot",
            snapshot_fetcher=fetch_snapshot,
        )
    )
    snapshotter = SnapshotEngine(
        bus,
        media,
        str(tmp_path / "data"),
    )
    event = Event(
        id="event-1",
        device_id="doorbell",
        area_id="front",
        timestamp=datetime.now(timezone.utc),
        event_type=event_type,
        event_state=EventState.ACTIVE,
        source="test:events",
        episode_id="episode-1",
    )

    async def capture_evidence(message: Message) -> None:
        evidence_messages.append(message)

    bus.subscribe("evidence.received", capture_evidence)
    await snapshotter.start()

    try:
        await bus.publish(
            Message(
                type="event.canonicalized",
                data={"result": CanonicalEventResult(event=event, created=True)},
            )
        )
        for _ in range(30):
            if evidence_messages or expected_snapshots == 0:
                break
            await asyncio.sleep(0.01)

        assert len(evidence_messages) == expected_snapshots
        assert fetches == expected_snapshots
        if evidence_messages:
            payload = evidence_messages[0].data
            assert payload["evidence"]["event_id"] == event.id
            assert payload["evidence"]["episode_id"] == event.episode_id
            assert payload["evidence"]["metadata"]["origin"] == ("hikvision-sdk-snapshot:snapshot")
            assert payload["evidence"]["metadata"]["requested_for"] == event.id
            assert Path(payload["evidence"]["file_path"]).read_bytes() == expected_bytes
    finally:
        await snapshotter.stop()
        bus.unsubscribe("evidence.received", capture_evidence)
