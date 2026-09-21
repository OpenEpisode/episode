from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from episode.api.routes import create_api
from episode.capture_profiles import CaptureProfileError, CaptureProfileService
from episode.config import EpisodeConfig
from episode.domain.event_filter import (
    ATTACHMENT_ATTACHED,
    ATTACHMENT_NO_OPEN_EPISODE,
    DEVICE_SELECTABLE_EVENT_CLASSES,
    EVENT_CLASS_ACCESS,
    EVENT_CLASS_BY_TYPE,
    EVENT_CLASS_CONDITION,
    EVENT_CLASS_DETECTION,
    EVENT_CLASS_HEARTBEAT,
    EVENT_CLASS_MOTION,
    EVENT_CLASS_SECURITY,
    EVENT_CLASS_UNKNOWN,
    EVENT_CLASSES,
    FILTER_SOURCE_DEVICE,
    FILTER_SOURCE_PROFILE,
    FILTERABLE_EVENT_CLASSES,
    FILTERED_REASON,
    PROFILE_SELECTABLE_EVENT_CLASSES,
    decode_device_filter,
    encode_device_filter,
    event_class,
    is_filterable,
    normalize_device_selector,
    normalize_selector,
    selectable_event_classes,
    validate_selector,
)
from episode.domain.models import (
    Area,
    Device,
    EpisodeState,
    Event,
    EventState,
    IngestionReceipt,
)
from episode.engine.bus import EventBus
from episode.engine.engine import EpisodeEngine
from episode.inventory import InventoryService
from episode.storage.repository import Repository

GENERIC_OBSERVATIONS = [EVENT_CLASS_MOTION, EVENT_CLASS_HEARTBEAT, EVENT_CLASS_CONDITION]

OTHER_EVENT_TYPES = (
    "tamper_detection",
    "tampering_detection",
    "video_loss",
    "digital_input",
    "door_access",
    "card",
    "line_crossing_detection",
    "human_detection",
    "vehicle_detection",
    "pet_detection",
    "mystery_event",
)


@pytest_asyncio.fixture
async def filter_context(tmp_path):
    repo = Repository(EpisodeConfig(data_dir=str(tmp_path), db_path=str(tmp_path / "episode.db")))
    await repo.initialize()
    await repo.upsert_area(Area(id="front", name="Front"))
    await repo.upsert_area(Area(id="other", name="Other"))
    await repo.upsert_device(
        Device(id="camera", name="Camera", device_type="camera", area_id="front")
    )
    service = CaptureProfileService(repo)
    yield repo, service
    await repo.close()


async def stored_receipt(repo: Repository, received_at: datetime) -> IngestionReceipt:
    """Persist a Receipt so ingress time, not wall-clock time, drives lifecycle."""
    receipt = IngestionReceipt(source="test", received_at=received_at)
    await repo.create_ingestion_receipt(receipt)
    return receipt


def make_event(device_id="camera", event_type="motion_detection", **kwargs):
    return Event(
        device_id=device_id,
        area_id=kwargs.pop("area_id", "front"),
        event_type=event_type,
        event_state=EventState.ACTIVE,
        source="test",
        **kwargs,
    )


async def activate(repo: Repository, profile_id: str) -> None:
    await repo.activate_capture_profile(profile_id, source="test")


# --- Domain: every member sits in exactly one class ---


def test_every_mapped_type_has_exactly_one_class():
    # Consistency rule: nothing is privileged. Every class is selectable at both
    # levels, so a selector means the same thing whatever it names.
    assert FILTERABLE_EVENT_CLASSES == EVENT_CLASSES
    assert PROFILE_SELECTABLE_EVENT_CLASSES == EVENT_CLASSES
    assert DEVICE_SELECTABLE_EVENT_CLASSES == EVENT_CLASSES
    assert event_class("mystery") == EVENT_CLASS_UNKNOWN
    assert event_class("") == EVENT_CLASS_UNKNOWN
    assert event_class(None) == EVENT_CLASS_UNKNOWN


def test_class_membership_matches_the_policy_table():
    expected = {
        "motion_detection": EVENT_CLASS_MOTION,
        "system": EVENT_CLASS_HEARTBEAT,
        "battery_status": EVENT_CLASS_HEARTBEAT,
        "audio_detection": EVENT_CLASS_CONDITION,
        "battery_low": EVENT_CLASS_SECURITY,
        "tamper_detection": EVENT_CLASS_SECURITY,
        "tampering_detection": EVENT_CLASS_SECURITY,
        "video_loss": EVENT_CLASS_SECURITY,
        # A wired contact reports a signal the operator chose to raise, so it
        # sits with the operator-authored rules rather than with tamper.
        "digital_input": EVENT_CLASS_DETECTION,
        "human_detection": EVENT_CLASS_DETECTION,
        "door_access": EVENT_CLASS_ACCESS,
        "card": EVENT_CLASS_ACCESS,
    }
    assert {event_class(name) for name in expected} == {
        EVENT_CLASS_MOTION,
        EVENT_CLASS_HEARTBEAT,
        EVENT_CLASS_CONDITION,
        EVENT_CLASS_SECURITY,
        EVENT_CLASS_DETECTION,
        EVENT_CLASS_ACCESS,
    }
    for name, cls in expected.items():
        assert EVENT_CLASS_BY_TYPE[name] == cls


def test_levels_offer_the_same_classes():
    assert selectable_event_classes("profile") == PROFILE_SELECTABLE_EVENT_CLASSES
    assert selectable_event_classes("device") == DEVICE_SELECTABLE_EVENT_CLASSES
    assert selectable_event_classes("profile") == selectable_event_classes("device")
    with pytest.raises(ValueError, match="Unknown event filter level"):
        selectable_event_classes("area")


def test_every_class_is_selectable_at_both_levels():
    """No class is privileged and no level is more capable than the other."""
    for cls in sorted(EVENT_CLASSES):
        assert validate_selector([cls], level="profile") == [cls]
        assert validate_selector([cls], level="device") == [cls]
    widest = sorted(EVENT_CLASSES)
    assert validate_selector(EVENT_CLASSES, level="profile") == widest
    assert validate_selector(EVENT_CLASSES, level="device") == widest


def test_unknown_class_and_duplicates_are_rejected():
    with pytest.raises(ValueError, match="Unknown event class"):
        validate_selector(["spatial"])
    with pytest.raises(ValueError, match="selected more than once"):
        validate_selector([EVENT_CLASS_MOTION, " motion "])
    with pytest.raises(ValueError, match="Unknown event class"):
        validate_selector("motion security")
    with pytest.raises(ValueError, match="list of event class names"):
        validate_selector({"motion": True})


def test_validation_returns_a_canonical_sorted_list():
    assert validate_selector([EVENT_CLASS_CONDITION, EVENT_CLASS_MOTION]) == [
        EVENT_CLASS_CONDITION,
        EVENT_CLASS_MOTION,
    ]
    assert validate_selector(None) == []
    assert validate_selector([]) == []
    assert validate_selector("[]") == []
    for legacy_value in (True, False, "enabled", "disabled", "inherit"):
        with pytest.raises(ValueError):
            validate_selector(legacy_value)


def test_normalize_handles_every_stored_and_wire_shape():
    assert normalize_selector(None) == frozenset()
    assert normalize_selector('["heartbeat","motion"]') == {
        EVENT_CLASS_MOTION,
        EVENT_CLASS_HEARTBEAT,
    }
    assert normalize_selector([EVENT_CLASS_MOTION]) == {EVENT_CLASS_MOTION}
    # Damaged or unselectable data read back from storage means no filtering.
    assert normalize_selector(True) == frozenset()
    assert normalize_selector("enabled") == frozenset()
    assert normalize_selector("disabled") == frozenset()
    assert normalize_selector("garbage") == frozenset()
    assert normalize_selector('["motion"') == frozenset()
    assert normalize_selector(7) == frozenset()
    # Names the class model has never heard are dropped, never widened. Every
    # real class is a valid selection now, so only the nonsense entry goes.
    assert normalize_selector(["motion", "detection", "nonsense"]) == {
        EVENT_CLASS_MOTION,
        EVENT_CLASS_DETECTION,
    }


def test_normalize_device_selector_preserves_inherit():
    assert normalize_device_selector(None) is None
    assert normalize_device_selector("inherit") is None
    assert normalize_device_selector("") is None
    assert normalize_device_selector([]) == []
    assert normalize_device_selector([EVENT_CLASS_MOTION]) == [EVENT_CLASS_MOTION]
    assert encode_device_filter(None) == "inherit"
    assert encode_device_filter([]) == "[]"
    assert encode_device_filter([EVENT_CLASS_MOTION]) == '["motion"]'
    assert decode_device_filter("inherit") is None
    assert decode_device_filter("[]") == []
    assert decode_device_filter('["security","motion"]') == [
        EVENT_CLASS_MOTION,
        EVENT_CLASS_SECURITY,
    ]


def test_is_filterable_respects_class_and_selector():
    assert is_filterable("motion_detection", {EVENT_CLASS_MOTION}) is True
    assert is_filterable("system", {EVENT_CLASS_MOTION}) is False
    assert is_filterable("motion_detection", set()) is False
    # An empty selector filters nothing at all, whatever the type.
    for event_type in OTHER_EVENT_TYPES:
        assert is_filterable(event_type, frozenset()) is False, event_type
    # Any class can be suppressed once it is named, at either level.
    for event_type in OTHER_EVENT_TYPES:
        assert is_filterable(event_type, {event_class(event_type)}) is True, event_type
    assert is_filterable("mystery_event", {EVENT_CLASS_UNKNOWN}) is True


# --- Participation: the class gate and precedence ---


@pytest.mark.asyncio
async def test_explicit_generic_classes_leave_security_events_allowed(filter_context):
    """An explicit low-signal filter leaves unselected event classes allowed."""
    repo, service = filter_context
    profile = await service.create_profile("Night", ["camera"], event_filter=GENERIC_OBSERVATIONS)
    await activate(repo, profile.id)

    for event_type in ("motion_detection", "system", "audio_detection"):
        decision, _targets = await service.evaluate_event(make_event("camera", event_type))
        assert decision.allowed is False, event_type
        assert decision.reason == FILTERED_REASON
        assert decision.filtered_event_type == event_type
        assert decision.filtered_event_class == event_class(event_type)
        assert decision.filter_source == FILTER_SOURCE_PROFILE
        assert decision.attachment is None

    for event_type in (*OTHER_EVENT_TYPES, "battery_low"):
        decision, _targets = await service.evaluate_event(make_event("camera", event_type))
        assert decision.allowed is True, event_type
        assert decision.reason == "device_in_profile"
        assert decision.filtered_event_type is None
        assert decision.filtered_event_class is None
        assert decision.filter_source is None


@pytest.mark.asyncio
async def test_selector_scopes_the_suppression(filter_context):
    repo, service = filter_context
    motion_only = await service.create_profile(
        "Motion only", ["camera"], event_filter=[EVENT_CLASS_MOTION]
    )
    await activate(repo, motion_only.id)
    assert (await service.evaluate_event(make_event("camera", "motion_detection")))[
        0
    ].allowed is False
    # Heartbeat is not selected, so a status observation still participates.
    assert (await service.evaluate_event(make_event("camera", "system")))[0].allowed is True

    with_heartbeat = await service.update_profile(
        motion_only.id,
        "Motion + status",
        ["camera"],
        event_filter=[EVENT_CLASS_MOTION, "heartbeat"],
    )
    assert with_heartbeat.event_filter == [EVENT_CLASS_HEARTBEAT, EVENT_CLASS_MOTION]
    assert (await service.evaluate_event(make_event("camera", "system")))[0].allowed is False


@pytest.mark.asyncio
async def test_device_level_security_selection_suppresses_tamper_only(filter_context):
    repo, service = filter_context
    await repo.upsert_device(
        Device(
            id="camera",
            name="Camera",
            device_type="camera",
            area_id="front",
            event_filter=[EVENT_CLASS_MOTION, EVENT_CLASS_SECURITY],
        )
    )
    for event_type in ("tamper_detection", "video_loss", "motion_detection"):
        decision, _targets = await service.evaluate_event(make_event("camera", event_type))
        assert decision.allowed is False, event_type
        assert decision.filter_source == FILTER_SOURCE_DEVICE
    # Classes the selector does not name still participate.
    for event_type in ("human_detection", "door_access", "mystery_event"):
        decision, _targets = await service.evaluate_event(make_event("camera", event_type))
        assert decision.allowed is True, event_type


@pytest.mark.asyncio
async def test_device_selector_wins_in_both_directions(filter_context):
    repo, service = filter_context
    profile = await service.create_profile(
        "Night", ["camera"], event_filter=[EVENT_CLASS_MOTION, EVENT_CLASS_HEARTBEAT]
    )
    await activate(repo, profile.id)

    # An explicit negative wins over a filtering profile.
    await repo.upsert_device(
        Device(id="camera", name="Camera", device_type="camera", area_id="front", event_filter=[])
    )
    decision, _targets = await service.evaluate_event(make_event("camera", "motion_detection"))
    assert decision.allowed is True
    assert decision.reason == "device_in_profile"

    # A narrower Device selector applies instead of the wider profile selector.
    await repo.upsert_device(
        Device(
            id="camera",
            name="Camera",
            device_type="camera",
            area_id="front",
            event_filter=[EVENT_CLASS_HEARTBEAT],
        )
    )
    assert (await service.evaluate_event(make_event("camera", "motion_detection")))[
        0
    ].allowed is True
    assert (await service.evaluate_event(make_event("camera", "system")))[0].allowed is False

    # Inherit falls back to the profile selector again.
    await repo.upsert_device(
        Device(
            id="camera",
            name="Camera",
            device_type="camera",
            area_id="front",
            event_filter=None,
        )
    )
    assert (await service.evaluate_event(make_event("camera", "motion_detection")))[
        0
    ].allowed is False


@pytest.mark.asyncio
async def test_membership_reasons_are_unchanged(filter_context):
    repo, service = filter_context
    profile = await service.create_profile(
        "Night", ["camera"], event_filter=[EVENT_CLASS_MOTION, EVENT_CLASS_HEARTBEAT]
    )
    await activate(repo, profile.id)
    await repo.upsert_device(
        Device(id="unlisted", name="Unlisted", device_type="camera", area_id="front")
    )
    excluded, _targets = await service.evaluate_event(make_event("unlisted", "motion_detection"))
    assert excluded.allowed is False
    # Membership exclusion is a different statement from type suppression.
    assert excluded.reason == "device_not_in_profile"
    assert excluded.filtered_event_type is None
    assert excluded.filter_source is None

    disabled_target, _targets = await service.evaluate_event(make_event("missing", "system"))
    assert disabled_target.reason == "device_not_found"


@pytest.mark.asyncio
async def test_inactive_events_skip_the_gate(filter_context):
    repo, service = filter_context
    profile = await service.create_profile(
        "Night", ["camera"], event_filter=[EVENT_CLASS_MOTION, EVENT_CLASS_HEARTBEAT]
    )
    await activate(repo, profile.id)
    with pytest.raises(ValueError, match="active Events"):
        await service.evaluate_event(
            Event(
                device_id="camera",
                area_id="front",
                event_type="motion_detection",
                event_state=EventState.INACTIVE,
                source="test",
            )
        )


@pytest.mark.asyncio
async def test_profile_service_accepts_any_class_selector(filter_context):
    """Consistency rule: a profile is not blocked from a class a Device may pick.

    Suppressing a security class across a group is a costly decision, so it is
    surfaced in the profile summary and the Device list marker rather than
    refused by validation.
    """
    _repo, service = filter_context
    profile = await service.create_profile(
        "Quiet group", ["camera"], event_filter=[EVENT_CLASS_SECURITY]
    )
    assert profile.event_filter == [EVENT_CLASS_SECURITY]
    updated = await service.update_profile(
        profile.id, "Renamed", ["camera"], event_filter=sorted(EVENT_CLASSES)
    )
    assert updated.event_filter == sorted(EVENT_CLASSES)


@pytest.mark.asyncio
async def test_service_rejects_unknown_class(filter_context):
    _repo, service = filter_context
    with pytest.raises(CaptureProfileError, match="Unknown event class"):
        await service.create_profile("Bad", ["camera"], event_filter=["spatial"])


# --- Pipeline: attachment records activity without driving capture ---


@pytest.mark.asyncio
async def test_filtered_event_attaches_to_an_open_episode_without_extending_it(filter_context):
    repo, service = filter_context
    profile = await service.create_profile("Night", ["camera"], event_filter=[EVENT_CLASS_MOTION])
    await activate(repo, profile.id)
    engine = EpisodeEngine(repo, EventBus(), timeout=30, capture_profiles=service)
    await engine.start()
    try:
        opening = await engine.ingest_event(make_event("camera", "human_detection"))
        episode_id = opening.event.episode_id
        before = await repo.get_episode(episode_id)

        filtered = await engine.ingest_event(make_event("camera", "motion_detection"))

        stored = await repo.get_event(filtered.event.id)
        assert stored.episode_id == episode_id
        assert stored.participation.allowed is False
        assert stored.participation.attachment == ATTACHMENT_ATTACHED
        after = await repo.get_episode(episode_id)
        # Capture lifetime is decided only by Events allowed to drive it.
        assert after.minimum_end_at == before.minimum_end_at
        assert after.last_activity_at == before.last_activity_at
        assert after.state == EpisodeState.ACTIVE
        assert after.event_count == before.event_count + 1
        assert await repo.list_evidence() == []
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_filtered_event_does_not_restart_a_quiescent_episode(filter_context):
    repo, service = filter_context
    profile = await service.create_profile("Night", ["camera"], event_filter=[EVENT_CLASS_MOTION])
    await activate(repo, profile.id)
    engine = EpisodeEngine(repo, EventBus(), timeout=30, capture_profiles=service)
    await engine.start()
    try:
        await repo.upsert_device(
            Device(
                id="camera",
                name="Camera",
                device_type="camera",
                area_id="front",
                activity_window_seconds=1,
            )
        )
        base = datetime.now(timezone.utc) - timedelta(seconds=3)
        opening = await engine.ingest_event(
            make_event("camera", "human_detection", timestamp=base),
            receipt=await stored_receipt(repo, base),
        )
        episode_id = opening.event.episode_id
        await repo.transition_timed_out_episodes(
            timeout=30,
            quiescent_grace_seconds=5,
            now=base + timedelta(seconds=2),
        )
        before = await repo.get_episode(episode_id)
        assert before.state == EpisodeState.QUIESCENT

        await engine.ingest_event(
            make_event("camera", "motion_detection", timestamp=base + timedelta(seconds=3)),
            receipt=await stored_receipt(repo, base + timedelta(seconds=3)),
        )
        after = await repo.get_episode(episode_id)
        assert after.state == EpisodeState.QUIESCENT
        assert after.minimum_end_at == before.minimum_end_at
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_filtered_event_without_an_open_episode_stays_unassigned(filter_context):
    repo, service = filter_context
    profile = await service.create_profile("Night", ["camera"], event_filter=[EVENT_CLASS_MOTION])
    await activate(repo, profile.id)
    engine = EpisodeEngine(repo, EventBus(), timeout=30, capture_profiles=service)
    await engine.start()
    try:
        created_events = []

        async def observe(message):
            created_events.append(message.type)

        bus_engine = engine
        bus_engine._bus.subscribe("episode.created", observe)

        filtered = await engine.ingest_event(make_event("camera", "motion_detection"))
        stored = await repo.get_event(filtered.event.id)
        assert stored.episode_id is None
        assert stored.participation.attachment == ATTACHMENT_NO_OPEN_EPISODE
        assert await repo.list_episodes() == []
        assert "episode.created" not in created_events

        # A later security-bearing detection from the same camera still opens one.
        opening = await engine.ingest_event(make_event("camera", "tamper_detection"))
        assert opening.event.episode_id is not None
        assert await repo.list_episodes() != []
        # ... and the earlier filtered observation is not retroactively attached.
        assert (await repo.get_event(stored.id)).episode_id is None
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_paired_inactive_event_attaches_through_the_same_episode(filter_context):
    repo, service = filter_context
    profile = await service.create_profile("Night", ["camera"], event_filter=[EVENT_CLASS_MOTION])
    await activate(repo, profile.id)
    engine = EpisodeEngine(repo, EventBus(), timeout=30, capture_profiles=service)
    await engine.start()
    try:
        base = datetime.now(timezone.utc)
        opening = await engine.ingest_event(make_event("camera", "human_detection", timestamp=base))
        active_motion = await engine.ingest_event(
            make_event("camera", "motion_detection", timestamp=base + timedelta(seconds=1))
        )
        assert active_motion.event.episode_id == opening.event.episode_id
        inactive_motion = await engine.ingest_event(
            Event(
                device_id="camera",
                area_id="front",
                event_type="motion_detection",
                event_state=EventState.INACTIVE,
                timestamp=base + timedelta(seconds=2),
                source="test",
            )
        )
        # Inactive transitions pair to the Episode via the active event, and the
        # class gate never runs for them.
        assert inactive_motion.event.episode_id == opening.event.episode_id
        assert inactive_motion.event.participation is None
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_later_allowed_event_extends_the_episode(filter_context):
    repo, service = filter_context
    profile = await service.create_profile(
        "Night", ["camera"], event_filter=[EVENT_CLASS_MOTION, EVENT_CLASS_HEARTBEAT]
    )
    await activate(repo, profile.id)
    engine = EpisodeEngine(repo, EventBus(), timeout=30, capture_profiles=service)
    await engine.start()
    try:
        base = datetime.now(timezone.utc)
        first = await engine.ingest_event(make_event("camera", "human_detection", timestamp=base))
        deadline_after_first = (await repo.get_episode(first.event.episode_id)).minimum_end_at
        for _ in range(3):
            await engine.ingest_event(make_event("camera", "motion_detection"))
        assert (await repo.get_episode(first.event.episode_id)).minimum_end_at == (
            deadline_after_first
        )
        second = await engine.ingest_event(
            make_event("camera", "vehicle_detection", timestamp=base + timedelta(seconds=4))
        )
        assert second.event.episode_id == first.event.episode_id
        assert (await repo.get_episode(first.event.episode_id)).minimum_end_at > (
            deadline_after_first
        )
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_filtered_event_stays_preserved_and_queryable(filter_context):
    repo, service = filter_context
    profile = await service.create_profile("Night", ["camera"], event_filter=[EVENT_CLASS_MOTION])
    await activate(repo, profile.id)
    result, created = await service.canonicalize_event(make_event("camera", "motion_detection"))
    assert created is True
    stored = await repo.get_event(result.id)
    assert stored is not None
    assert stored.event_type == "motion_detection"
    assert stored.participation.reason == FILTERED_REASON
    assert stored.participation.filtered_event_class == EVENT_CLASS_MOTION
    assert stored.eligible_recording_device_ids == []


# --- Storage: round-trip and restart ---


@pytest.mark.asyncio
async def test_profile_and_device_selector_round_trip(filter_context):
    repo, service = filter_context
    created = await service.create_profile(
        "Night", ["camera"], event_filter=[EVENT_CLASS_CONDITION, EVENT_CLASS_MOTION]
    )
    fetched = await service.get_profile(created.id)
    assert fetched.event_filter == [EVENT_CLASS_CONDITION, EVENT_CLASS_MOTION]

    cleared = await service.update_profile(created.id, "Night", ["camera"], event_filter=[])
    assert cleared.event_filter == []
    assert (await service.get_profile(created.id)).event_filter == []

    await repo.upsert_device(
        Device(
            id="camera",
            name="Camera",
            device_type="camera",
            area_id="front",
            event_filter=[EVENT_CLASS_SECURITY],
        )
    )
    assert (await repo.get_device("camera")).event_filter == [EVENT_CLASS_SECURITY]
    # A Device with no selector stores the inherit sentinel, not an empty list.
    assert (await repo.get_device("camera")).event_filter is not None
    await repo.upsert_device(
        Device(id="sensor", name="Sensor", device_type="sensor", area_id="front")
    )
    assert (await repo.get_device("sensor")).event_filter is None


@pytest.mark.asyncio
async def test_builtin_profile_has_an_empty_selector(filter_context):
    _repo, service = filter_context
    builtin = await service.get_profile("all-devices")
    assert builtin.builtin is True
    assert builtin.event_filter == []


@pytest.mark.asyncio
async def test_device_selector_survives_a_restart(tmp_path):
    config = EpisodeConfig(data_dir=str(tmp_path), db_path=str(tmp_path / "episode.db"))
    repo = Repository(config)
    await repo.initialize()
    await repo.upsert_area(Area(id="front", name="Front"))
    await repo.upsert_device(
        Device(
            id="camera",
            name="Camera",
            device_type="camera",
            area_id="front",
            event_filter=[EVENT_CLASS_MOTION, EVENT_CLASS_SECURITY],
        )
    )
    await repo.close()

    reopened = Repository(config)
    await reopened.initialize()
    try:
        device = await reopened.get_device("camera")
        assert device.event_filter == [EVENT_CLASS_MOTION, EVENT_CLASS_SECURITY]
    finally:
        await reopened.close()


# --- API contract ---


@pytest.mark.asyncio
async def test_api_carries_event_filter_at_both_levels(filter_context):
    repo, service = filter_context
    app = create_api(
        repo,
        str(repo._data_dir),
        inventory=InventoryService(repo),
        capture_profiles=service,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/api/v1/capture-profiles",
            json={
                "name": "Night",
                "device_ids": ["camera"],
                "event_filter": ["motion"],
                "filter_generic_events": True,
            },
        )
        assert created.status_code == 201
        assert created.json()["event_filter"] == ["motion"]
        assert "filter_generic_events" not in created.json()

        old_profile_field = await client.post(
            "/api/v1/capture-profiles",
            json={
                "name": "Old profile field",
                "device_ids": ["camera"],
                "filter_generic_events": True,
            },
        )
        assert old_profile_field.status_code == 201
        assert old_profile_field.json()["event_filter"] == []
        assert "filter_generic_events" not in old_profile_field.json()

        accepted = await client.post(
            "/api/v1/capture-profiles",
            json={"name": "Quiet group", "device_ids": ["camera"], "event_filter": ["security"]},
        )
        # Every class is selectable at either level; only unknown names are refused.
        assert accepted.status_code == 201
        assert accepted.json()["event_filter"] == ["security"]

        unknown = await client.post(
            "/api/v1/capture-profiles",
            json={"name": "Junk", "device_ids": ["camera"], "event_filter": ["spatial"]},
        )
        assert unknown.status_code == 422

        # A Device may opt into suppressing security, and the selector round-trips.
        device = await client.post(
            "/api/v1/devices",
            json={
                "id": "gate-cam",
                "name": "Gate cam",
                "area_id": "front",
                "ip_address": "192.0.2.20",
                "episode_policy": {
                    "event_filter": ["motion", "security"],
                    "generic_event_filter": "disabled",
                },
            },
        )
        assert device.status_code == 201
        policy = device.json()["configuration"]["episode_policy"]
        assert policy["event_filter"] == ["motion", "security"]
        assert "generic_event_filter" not in policy

        # Old fields are not translated into the new selector.
        old_field = await client.post(
            "/api/v1/devices",
            json={
                "id": "old-field-cam",
                "name": "Old field cam",
                "area_id": "front",
                "ip_address": "192.0.2.21",
                "episode_policy": {"generic_event_filter": "enabled"},
            },
        )
        assert old_field.status_code == 201
        policy = old_field.json()["configuration"]["episode_policy"]
        assert policy["event_filter"] is None
        assert "generic_event_filter" not in policy
        inherit = await client.post(
            "/api/v1/devices",
            json={
                "id": "plain-cam",
                "name": "Plain cam",
                "area_id": "front",
                "ip_address": "192.0.2.22",
            },
        )
        assert inherit.json()["configuration"]["episode_policy"]["event_filter"] is None


@pytest.mark.asyncio
async def test_participation_projection_carries_the_new_fields(filter_context):
    repo, service = filter_context
    profile = await service.create_profile("Night", ["camera"], event_filter=[EVENT_CLASS_MOTION])
    await activate(repo, profile.id)
    engine = EpisodeEngine(repo, EventBus(), timeout=30, capture_profiles=service)
    await engine.start()
    try:
        opening = await engine.ingest_event(make_event("camera", "human_detection"))
        filtered = await engine.ingest_event(
            make_event("camera", "motion_detection"),
        )
        app = create_api(repo, str(repo._data_dir), capture_profiles=service)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            body = (await client.get(f"/api/v1/events/{filtered.event.id}")).json()
            participation = body["participation"]
            assert participation["allowed"] is False
            assert participation["reason"] == FILTERED_REASON
            assert participation["filtered_event_type"] == "motion_detection"
            assert participation["filtered_event_class"] == EVENT_CLASS_MOTION
            assert participation["filter_source"] == FILTER_SOURCE_PROFILE
            assert participation["attachment"] == ATTACHMENT_ATTACHED
            assert participation["profile_name"] == "Night"
            # The filtered Event is attributed to the open Episode ...
            assert body["episode_id"] == opening.event.episode_id
            # ... and never leaks internal target identifiers.
            assert "eligible_recording_device_ids" not in body
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_portable_manifest_carries_the_filter_decision(filter_context):
    """§11: the decision must stay readable without the database or the UI."""
    repo, service = filter_context
    profile = await service.create_profile("Night", ["camera"], event_filter=[EVENT_CLASS_MOTION])
    await activate(repo, profile.id)
    engine = EpisodeEngine(repo, EventBus(), timeout=30, capture_profiles=service)
    await engine.start()
    try:
        opening = await engine.ingest_event(make_event("camera", "human_detection"))
        await engine.ingest_event(make_event("camera", "motion_detection"))
        await repo.refresh_episode_manifest(opening.event.episode_id)
    finally:
        await engine.stop()

    manifest_path = Path(repo._data_dir) / "episodes" / opening.event.episode_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    by_type = {item["type"]: item for item in manifest["events"]}

    allowed = by_type["human_detection"]["participation"]
    assert allowed["allowed"] is True
    assert allowed["filtered_event_class"] is None
    assert allowed["attachment"] is None

    filtered = by_type["motion_detection"]["participation"]
    assert filtered["allowed"] is False
    assert filtered["reason"] == FILTERED_REASON
    assert filtered["filtered_event_type"] == "motion_detection"
    assert filtered["filtered_event_class"] == EVENT_CLASS_MOTION
    assert filtered["filter_source"] == FILTER_SOURCE_PROFILE
    assert filtered["attachment"] == ATTACHMENT_ATTACHED
    assert filtered["profile_name"] == "Night"
    assert filtered["evaluated_at"]


@pytest.mark.asyncio
async def test_a_damaged_stored_selector_falls_back_to_inherit(filter_context):
    """A corrupt row must not widen filtering, nor invent an operator choice (§12)."""
    repo, service = filter_context
    profile = await service.create_profile("Night", ["camera"], event_filter=[EVENT_CLASS_MOTION])
    await repo.upsert_device(
        Device(
            id="sensor",
            name="Sensor",
            device_type="sensor",
            area_id="front",
            event_filter=[EVENT_CLASS_HEARTBEAT],
        )
    )

    # Damage the stored rows directly: the point is a value no decoder can read.
    await repo._conn.execute("UPDATE devices SET event_filter = '{' WHERE id = 'sensor'")  # noqa: SLF001
    await repo._conn.commit()
    await repo._capture_profile_conn.execute(  # noqa: SLF001
        "UPDATE capture_profiles SET event_filter = 'not json'"
    )
    await repo._capture_profile_conn.commit()

    # Reading the profile back yields "no filtering", not an error and not a
    # guessed selector.
    assert (await service.get_profile(profile.id)).event_filter == []
    # An unreadable Device value decodes to inherit, not to an invented
    # "filters nothing" choice and not to a guessed class list: a damaged row is
    # not an operator statement, so the active profile stays authoritative and the
    # anomaly heals when the row is saved again (§15 Q5, decided 2026-09-19).
    assert (await repo.get_device("sensor")).event_filter is None
    # Ingestion still runs: the profile row is damaged too, so nothing is
    # selected and motion participates.
    await activate(repo, profile.id)
    result, created = await service.canonicalize_event(make_event("camera", "motion_detection"))
    assert created is True
    assert result.participation.allowed is True
    assert result.participation.reason == "device_in_profile"

    # The point of inherit rather than the explicit negative: once the profile is
    # readable and selects motion, the damaged Device follows it instead of being
    # pinned to "no filtering" by its own broken row.
    await repo._capture_profile_conn.execute(  # noqa: SLF001
        "UPDATE capture_profiles SET event_filter = ?", (json.dumps([EVENT_CLASS_MOTION]),)
    )
    await repo._capture_profile_conn.commit()
    filtered = await service.canonicalize_event(
        make_event(
            "camera",
            "motion_detection",
            timestamp=datetime.now(tz=timezone.utc) + timedelta(seconds=5),
        )
    )
    assert filtered[0].participation.allowed is False
    assert filtered[0].participation.reason == FILTERED_REASON
    assert filtered[0].participation.filter_source == FILTER_SOURCE_PROFILE


@pytest.mark.asyncio
async def test_a_finalizing_episode_is_not_reopened_by_a_filtered_event(filter_context):
    """FINALIZING is a terminal association barrier for filtered Events too (§8).

    Main's sealing work made `finalizing` a real state, and attach-not-drive reads
    the same open-Episode query. If that query ever widened, a suppressed heartbeat
    would silently resurrect an Episode on its way to being closed.
    """
    repo, service = filter_context
    profile = await service.create_profile("Night", ["camera"], event_filter=[EVENT_CLASS_MOTION])
    await activate(repo, profile.id)
    engine = EpisodeEngine(repo, EventBus(), timeout=30, capture_profiles=service)
    await engine.start()
    try:
        opening = await engine.ingest_event(
            make_event("camera", "human_detection"),
            receipt=await stored_receipt(repo, datetime.now(tz=timezone.utc)),
        )
        episode_id = opening.event.episode_id
        await repo.update_episode_state(episode_id, EpisodeState.FINALIZING)

        filtered = await engine.ingest_event(
            make_event("camera", "motion_detection"),
            receipt=await stored_receipt(repo, datetime.now(tz=timezone.utc)),
        )

        stored = await repo.get_event(filtered.event.id)
        assert stored.episode_id is None
        assert stored.participation.allowed is False
        assert stored.participation.attachment == ATTACHMENT_NO_OPEN_EPISODE
        assert (await repo.get_episode(episode_id)).state == EpisodeState.FINALIZING
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_setup_state_and_event_filter_are_independent_fields(filter_context):
    """Onboarding state must not borrow the filter column, nor reset it (§12).

    A Device held for later setup still carries its own selector, and moving it to
    `ready` must not silently widen or clear filtering — `[]` means "this camera
    filters nothing", so a reset would be an unrequested policy change.
    """
    repo, _service = filter_context
    app = create_api(repo, str(repo._data_dir), inventory=InventoryService(repo))  # noqa: SLF001
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        draft = await client.post(
            "/api/v1/devices",
            json={
                "id": "draft-cam",
                "name": "Draft cam",
                "area_id": "front",
                "ip_address": "192.0.2.30",
                "setup_state": "needs_setup",
                "episode_policy": {"event_filter": ["heartbeat"]},
            },
        )
        assert draft.status_code == 201
        assert draft.json()["setup_state"] == "needs_setup"
        assert draft.json()["configuration"]["episode_policy"]["event_filter"] == ["heartbeat"]

        updated = await client.put(
            "/api/v1/devices/draft-cam",
            json={
                "id": "draft-cam",
                "name": "Draft cam",
                "area_id": "front",
                "ip_address": "192.0.2.30",
                "setup_state": "ready",
                "episode_policy": {"event_filter": ["heartbeat", "motion"]},
            },
        )
        assert updated.status_code == 200
        assert updated.json()["setup_state"] == "ready"

    restored = await repo.get_device("draft-cam")
    assert restored.setup_state == "ready"
    assert restored.event_filter == ["heartbeat", "motion"]
