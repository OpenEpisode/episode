from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from episode.api.routes import create_api
from episode.capture_profiles import CaptureProfileService
from episode.config import EpisodeConfig
from episode.domain.event_filter import (
    GENERIC_EVENT_TYPES,
    HIGH_LEVEL_EVENT_TYPES,
    is_generic_event_type,
)
from episode.domain.models import Area, Device, Event, EventState
from episode.engine.bus import EventBus
from episode.engine.engine import EpisodeEngine
from episode.storage.repository import Repository


@pytest_asyncio.fixture
async def filter_context(tmp_path):
    repo = Repository(EpisodeConfig(data_dir=str(tmp_path), db_path=str(tmp_path / "episode.db")))
    await repo.initialize()
    await repo.upsert_area(Area(id="front", name="Front"))
    await repo.upsert_device(
        Device(id="camera", name="Camera", device_type="camera", area_id="front")
    )
    service = CaptureProfileService(repo)
    yield repo, service
    await repo.close()


def make_event(device_id="camera", event_type="motion_detection"):
    return Event(
        device_id=device_id,
        area_id="front",
        event_type=event_type,
        event_state=EventState.ACTIVE,
        source="test",
    )


# --- Core domain classification ---


@pytest.mark.asyncio
async def test_is_generic_event_type_classifies_sets():
    for event_type in GENERIC_EVENT_TYPES:
        assert is_generic_event_type(event_type) is True
    for event_type in HIGH_LEVEL_EVENT_TYPES:
        assert is_generic_event_type(event_type) is False


@pytest.mark.asyncio
async def test_unknown_event_type_is_not_filtered():
    assert is_generic_event_type("human_detection") is False
    assert is_generic_event_type("vehicle_detection") is False
    assert is_generic_event_type("pet_detection") is False
    # Unknown / unrecognized types fail safe: treated as high-level.
    assert is_generic_event_type("mystery_event") is False
    assert is_generic_event_type("") is False


# --- Participation: generic events are filtered ---


@pytest.mark.asyncio
async def test_generic_event_filtered_with_profile_enabled(filter_context):
    repo, service = filter_context
    profile = await service.create_profile("Night", ["camera"], filter_generic_events=True)
    await service.activate_profile(profile.id)

    decision, _targets = await service.evaluate_event(make_event("camera", "motion_detection"))
    assert decision.allowed is False
    assert decision.reason == "generic_event_filtered"
    assert decision.filtered_event_type == "motion_detection"


@pytest.mark.asyncio
async def test_high_level_detection_passes_with_filtering_enabled(filter_context):
    repo, service = filter_context
    profile = await service.create_profile("Night", ["camera"], filter_generic_events=True)
    await service.activate_profile(profile.id)

    for event_type in ("human_detection", "vehicle_detection", "pet_detection"):
        decision, _targets = await service.evaluate_event(make_event("camera", event_type))
        assert decision.allowed is True, event_type
        assert decision.reason == "device_in_profile"
        assert decision.filtered_event_type is None


@pytest.mark.asyncio
async def test_filtering_disabled_preserves_current_behavior(filter_context):
    repo, service = filter_context
    profile = await service.create_profile("Night", ["camera"], filter_generic_events=False)
    await service.activate_profile(profile.id)

    decision, _targets = await service.evaluate_event(make_event("camera", "motion_detection"))
    assert decision.allowed is True
    assert decision.reason == "device_in_profile"


# --- Camera-level overrides take priority (positive and negative) ---


@pytest.mark.asyncio
async def test_camera_enabled_override_wins_over_disabled_profile(filter_context):
    repo, service = filter_context
    profile = await service.create_profile("Night", ["camera"], filter_generic_events=False)
    await service.activate_profile(profile.id)
    # Camera explicitly enables filtering even though the profile does not.
    await repo.upsert_device(
        Device(
            id="camera",
            name="Camera",
            device_type="camera",
            area_id="front",
            generic_event_filter="enabled",
        )
    )

    decision, _targets = await service.evaluate_event(make_event("camera", "motion_detection"))
    assert decision.allowed is False
    assert decision.reason == "generic_event_filtered"
    assert decision.filtered_event_type == "motion_detection"


@pytest.mark.asyncio
async def test_camera_disabled_override_wins_over_enabled_profile(filter_context):
    repo, service = filter_context
    profile = await service.create_profile("Night", ["camera"], filter_generic_events=True)
    await service.activate_profile(profile.id)
    # Camera explicitly disables filtering even though the profile enables it.
    await repo.upsert_device(
        Device(
            id="camera",
            name="Camera",
            device_type="camera",
            area_id="front",
            generic_event_filter="disabled",
        )
    )

    decision, _targets = await service.evaluate_event(make_event("camera", "motion_detection"))
    assert decision.allowed is True
    assert decision.reason == "device_in_profile"


@pytest.mark.asyncio
async def test_camera_inherit_follows_profile(filter_context):
    repo, service = filter_context
    profile = await service.create_profile("Night", ["camera"], filter_generic_events=True)
    await service.activate_profile(profile.id)
    await repo.upsert_device(
        Device(
            id="camera",
            name="Camera",
            device_type="camera",
            area_id="front",
            generic_event_filter="inherit",
        )
    )

    decision, _targets = await service.evaluate_event(make_event("camera", "motion_detection"))
    assert decision.allowed is False
    assert decision.reason == "generic_event_filtered"


# --- Pipeline: filtered event does not create an Episode ---


@pytest.mark.asyncio
async def test_filtered_event_does_not_open_or_extend_episode(filter_context):
    repo, service = filter_context
    profile = await service.create_profile("Night", ["camera"], filter_generic_events=True)
    await service.activate_profile(profile.id)
    engine = EpisodeEngine(repo, EventBus(), timeout=30, capture_profiles=service)
    await engine.start()
    try:
        # Generic event is filtered: no Episode is created.
        filtered = await engine.ingest_event(make_event("camera", "motion_detection"))
        assert filtered.event.participation is not None
        assert filtered.event.participation.allowed is False
        assert filtered.event.participation.reason == "generic_event_filtered"
        assert filtered.event.episode_id is None
        assert await repo.list_episodes() == []

        # A later high-level detection from the same camera opens an Episode.
        high = await engine.ingest_event(make_event("camera", "human_detection"))
        assert high.event.participation is not None
        assert high.event.participation.allowed is True
        assert high.event.episode_id is not None
        episodes = await repo.list_episodes()
        assert len(episodes) == 1
        # The filtered generic Event never joined the Episode.
        stored_filtered = await repo.get_event(filtered.event.id)
        assert stored_filtered.episode_id is None
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_filtered_event_is_still_preserved_and_queryable(filter_context):
    repo, service = filter_context
    profile = await service.create_profile("Night", ["camera"], filter_generic_events=True)
    await service.activate_profile(profile.id)
    event = make_event("camera", "motion_detection")
    result, created = await service.canonicalize_event(event)
    assert created is True
    assert result.participation is not None
    assert result.participation.allowed is False
    # Still persisted and queryable.
    stored = await repo.get_event(result.id)
    assert stored is not None
    assert stored.event_type == "motion_detection"
    assert stored.participation.reason == "generic_event_filtered"


# --- Storage: round-trip of new fields ---


@pytest.mark.asyncio
async def test_capture_profile_filter_round_trip(filter_context):
    repo, service = filter_context
    created = await service.create_profile("Night", ["camera"], filter_generic_events=True)
    fetched = await service.get_profile(created.id)
    assert fetched.filter_generic_events is True

    updated = await service.update_profile(
        created.id, "Day", ["camera"], filter_generic_events=False
    )
    assert updated.filter_generic_events is False
    fetched = await service.get_profile(created.id)
    assert fetched.filter_generic_events is False


@pytest.mark.asyncio
async def test_device_generic_event_filter_round_trip(filter_context):
    repo, service = filter_context
    await repo.upsert_device(
        Device(
            id="camera",
            name="Camera",
            device_type="camera",
            area_id="front",
            generic_event_filter="enabled",
        )
    )
    fetched = await repo.get_device("camera")
    assert fetched.generic_event_filter == "enabled"
    # Default for a new Device is inherit.
    await repo.upsert_device(
        Device(id="sensor", name="Sensor", device_type="sensor", area_id="front")
    )
    fetched = await repo.get_device("sensor")
    assert fetched.generic_event_filter == "inherit"


@pytest.mark.asyncio
async def test_additive_schema_columns_applied_to_existing_database(tmp_path):
    """A pre-existing database gains the new columns with safe defaults."""
    db_path = tmp_path / "existing.db"
    # Create a minimal pre-change database with the old schema.
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE areas (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, location TEXT NOT NULL DEFAULT '',
            metadata TEXT NOT NULL DEFAULT '{}', enabled INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE devices (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, device_type TEXT NOT NULL,
            area_id TEXT NOT NULL, capabilities TEXT NOT NULL DEFAULT '[]',
            ip_address TEXT NOT NULL DEFAULT '', username TEXT NOT NULL DEFAULT '',
            password TEXT NOT NULL DEFAULT '', configs TEXT NOT NULL DEFAULT '{}',
            activity_window_seconds INTEGER, metadata TEXT NOT NULL DEFAULT '{}',
            enabled INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE capture_profiles (
            id TEXT PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE,
            include_all_devices INTEGER NOT NULL DEFAULT 0,
            device_ids TEXT NOT NULL DEFAULT '[]',
            builtin INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO devices (id, name, device_type, area_id) VALUES ('camera', 'C', 'camera', 'a')"
    )
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    conn.execute(
        "INSERT INTO capture_profiles (id, name, created_at, updated_at) VALUES ('p', 'P', ?, ?)",
        (now, now),
    )
    conn.commit()
    conn.close()

    repo = Repository(EpisodeConfig(data_dir=str(tmp_path), db_path=str(db_path)))
    await repo.initialize()
    try:
        # Existing rows keep the safe defaults.
        device = await repo.get_device("camera")
        assert device.generic_event_filter == "inherit"
        profile = await repo.get_capture_profile("p")
        assert profile.filter_generic_events is False

        # The columns now exist so new writes round-trip.
        await repo.upsert_device(
            Device(
                id="camera",
                name="Camera",
                device_type="camera",
                area_id="a",
                generic_event_filter="enabled",
            )
        )
        fetched = await repo.get_device("camera")
        assert fetched.generic_event_filter == "enabled"
    finally:
        await repo.close()


# --- API contract: new fields exposed ---


@pytest.mark.asyncio
async def test_api_exposes_profile_and_participation_fields(filter_context):
    repo, service = filter_context
    profile = await service.create_profile("Night", ["camera"], filter_generic_events=True)
    await service.activate_profile(profile.id)
    event = make_event("camera", "motion_detection")
    await service.canonicalize_event(event)

    app = create_api(repo, str(repo._data_dir), capture_profiles=service)
    import httpx

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        profiles = (await client.get("/api/v1/capture-profiles")).json()
        night = next(item for item in profiles if item["id"] == profile.id)
        assert night["filter_generic_events"] is True

        projected = (await client.get(f"/api/v1/events/{event.id}")).json()
        assert projected["participation"]["allowed"] is False
        assert projected["participation"]["reason"] == "generic_event_filtered"
        assert projected["participation"]["filtered_event_type"] == "motion_detection"
