from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio

from episode.api.routes import create_api
from episode.capture_profiles import CaptureProfileService
from episode.config import EpisodeConfig
from episode.domain.models import (
    Area,
    CapabilityConfig,
    Device,
    Event,
    EventState,
    Evidence,
    IngestionReceipt,
    RawArtifact,
)
from episode.engine.bus import EventBus
from episode.engine.engine import EpisodeEngine
from episode.inventory import InventoryService
from episode.recording.targets import AreaRecordingTargetResolver
from episode.storage.repository import Repository


@pytest_asyncio.fixture
async def profile_context(tmp_path):
    repo = Repository(EpisodeConfig(data_dir=str(tmp_path), db_path=str(tmp_path / "episode.db")))
    await repo.initialize()
    await repo.upsert_area(Area(id="front", name="Front"))
    await repo.upsert_area(Area(id="other", name="Other"))
    await repo.upsert_device(
        Device(
            id="camera",
            name="Camera",
            device_type="camera",
            area_id="front",
            configs={
                "video": CapabilityConfig(settings={"recording_mode": "on_event"}),
            },
        )
    )
    await repo.upsert_device(
        Device(id="sensor", name="Sensor", device_type="sensor", area_id="front")
    )
    service = CaptureProfileService(repo)
    yield repo, service
    await repo.close()


@pytest.mark.asyncio
async def test_default_profile_is_dynamic_and_custom_empty_profile_excludes_events(
    profile_context,
):
    repo, service = profile_context
    profiles = await service.list_profiles()
    assert profiles[0].id == "all-devices"
    assert profiles[0].include_all_devices is True
    assert profiles[0].active is True

    disarmed = await service.create_profile("Disarmed", [])
    await service.activate_profile(disarmed.id)
    engine = EpisodeEngine(repo, EventBus(), timeout=30, capture_profiles=service)
    await engine.start()
    try:
        result = await engine.ingest_event(
            Event(
                device_id="camera",
                area_id="front",
                event_type="motion_detection",
                source="test",
            )
        )
        stored = await repo.get_event(result.event.id)
        assert stored is not None
        assert stored.episode_id is None
        assert stored.participation is not None
        assert stored.participation.allowed is False
        assert stored.participation.profile_id == disarmed.id
        assert stored.participation.reason == "device_not_in_profile"
        assert stored.eligible_recording_device_ids == []
        assert await repo.list_episodes() == []
        app = create_api(repo, str(repo._data_dir))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            projected = (await client.get(f"/api/v1/events/{stored.id}")).json()
        assert projected["participation"] == {
            "allowed": False,
            "profile_id": disarmed.id,
            "profile_name": "Disarmed",
            "reason": "device_not_in_profile",
            "evaluated_at": stored.participation.evaluated_at.isoformat(
                timespec="microseconds"
            ).replace("+00:00", "Z"),
            "filtered_event_type": None,
            "filtered_event_class": None,
            "filter_source": None,
            "attachment": None,
        }
        assert "eligible_recording_device_ids" not in projected
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_draft_device_cannot_emit_events_or_join_new_recording_targets(profile_context):
    repo, service = profile_context
    await repo.upsert_device(
        Device(
            id="draft-camera",
            name="Draft camera",
            device_type="camera",
            area_id="front",
            setup_state="needs_setup",
            configs={
                "video": CapabilityConfig(settings={"recording_mode": "on_episode"}),
            },
        )
    )
    engine = EpisodeEngine(repo, EventBus(), timeout=30, capture_profiles=service)
    await engine.start()
    try:
        with pytest.raises(ValueError, match="needs setup"):
            await engine.ingest_event(
                Event(device_id="draft-camera", area_id="front", event_type="motion")
            )
        assert await repo.list_events(device_id="draft-camera") == []
        with pytest.raises(ValueError, match="needs setup"):
            await engine.ingest_evidence(
                Evidence(device_id="draft-camera", area_id="front", evidence_type="snapshot")
            )
        assert await repo.list_evidence(device_id="draft-camera") == []

        accepted = await engine.ingest_event(
            Event(device_id="sensor", area_id="front", event_type="tripwire")
        )
        assert accepted.event.episode_id is not None
        assert accepted.event.eligible_recording_device_ids == []
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_draft_transition_does_not_rewrite_existing_recording_targets(profile_context):
    repo, service = profile_context
    engine = EpisodeEngine(repo, EventBus(), timeout=30, capture_profiles=service)
    await engine.start()
    try:
        accepted = await engine.ingest_event(
            Event(device_id="camera", area_id="front", event_type="motion_detection")
        )
        assert accepted.event.eligible_recording_device_ids == ["camera"]

        camera = await repo.get_device("camera")
        camera.setup_state = "needs_setup"
        await repo.upsert_device(camera)
        targets = await AreaRecordingTargetResolver(repo).resolve(accepted.event)
        assert [target.id for target in targets] == ["camera"]

        later = await engine.ingest_event(
            Event(device_id="sensor", area_id="front", event_type="tripwire")
        )
        assert later.event.eligible_recording_device_ids == []
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_excluded_event_keeps_raw_artifact_and_receipt(profile_context, tmp_path):
    repo, service = profile_context
    disarmed = await service.create_profile("Disarmed", [])
    await service.activate_profile(disarmed.id)
    payload_path = tmp_path / "raw.xml"
    payload_path.write_bytes(b"<event>raw</event>")
    artifact = RawArtifact(
        artifact_type="event_payload",
        file_path=str(payload_path),
        mime_type="application/xml",
        byte_size=payload_path.stat().st_size,
        sha256="a" * 64,
    )
    receipt = IngestionReceipt(source="test", received_at=datetime.now(timezone.utc))
    artifact, receipt = await repo.persist_delivery(artifact, receipt)
    engine = EpisodeEngine(repo, EventBus(), timeout=30, capture_profiles=service)
    await engine.start()
    try:
        result = await engine.ingest_event(
            Event(
                device_id="camera",
                area_id="front",
                event_type="motion_detection",
                source="test",
                raw_payload_path=artifact.file_path,
            ),
            receipt=receipt,
        )
        stored_receipt = await repo.get_ingestion_receipt(receipt.id)
        stored_artifact = await repo.get_raw_artifact(artifact.id)
        assert result.event.episode_id is None
        assert stored_receipt and stored_receipt.event_id == result.event.id
        assert stored_receipt.episode_id is None
        assert stored_artifact and stored_artifact.file_path == str(payload_path)
        assert payload_path.read_bytes() == b"<event>raw</event>"
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_excluded_event_does_not_extend_an_existing_episode(profile_context):
    repo, service = profile_context
    engine = EpisodeEngine(repo, EventBus(), timeout=30, capture_profiles=service)
    await engine.start()
    try:
        accepted = await engine.ingest_event(
            Event(
                device_id="camera",
                area_id="front",
                event_type="motion_detection",
                source="test",
            )
        )
        before = await repo.get_episode(accepted.event.episode_id)
        assert before is not None

        disarmed = await service.create_profile("Disarmed", [])
        await service.activate_profile(disarmed.id)
        excluded = await engine.ingest_event(
            Event(
                device_id="camera",
                area_id="front",
                event_type="human_detection",
                timestamp=accepted.event.timestamp + timedelta(seconds=10),
                source="test",
            )
        )
        after = await repo.get_episode(before.id)

        assert excluded.event.episode_id is None
        assert excluded.event.participation is not None
        assert excluded.event.participation.allowed is False
        assert after is not None
        assert after.last_event_time == before.last_event_time
        assert after.last_activity_at == before.last_activity_at
        assert after.minimum_end_at == before.minimum_end_at
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_inactive_event_attaches_to_historical_active_event_after_profile_switch(
    profile_context,
):
    repo, service = profile_context
    armed = await service.create_profile("Armed", ["camera"])
    await service.activate_profile(armed.id)
    engine = EpisodeEngine(repo, EventBus(), timeout=30, capture_profiles=service)
    await engine.start()
    try:
        active = await engine.ingest_event(
            Event(
                device_id="camera",
                area_id="front",
                event_type="motion_detection",
                event_state=EventState.ACTIVE,
                source="test",
            )
        )
        disarmed = await service.create_profile("Disarmed", [])
        await service.activate_profile(disarmed.id)
        inactive = await engine.ingest_event(
            Event(
                device_id="camera",
                area_id="front",
                event_type="motion_detection",
                event_state=EventState.INACTIVE,
                timestamp=active.event.timestamp + timedelta(seconds=1),
                source="test",
            )
        )
        assert inactive.event.episode_id == active.event.episode_id
        assert inactive.event.participation is None
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_duplicate_event_keeps_first_profile_decision(profile_context):
    repo, service = profile_context
    engine = EpisodeEngine(repo, EventBus(), timeout=30, capture_profiles=service)
    await engine.start()
    try:
        observed_at = datetime.now(timezone.utc)
        first = await engine.ingest_event(
            Event(
                device_id="camera",
                area_id="front",
                event_type="motion_detection",
                timestamp=observed_at,
                source="test:first",
            )
        )
        assert first.event.participation is not None
        assert first.event.participation.profile_id == "all-devices"

        disarmed = await service.create_profile("Disarmed", [])
        await service.activate_profile(disarmed.id)
        duplicate = await engine.ingest_event(
            Event(
                device_id="camera",
                area_id="front",
                event_type="motion_detection",
                timestamp=observed_at,
                source="test:duplicate",
            )
        )

        assert duplicate.created is False
        assert duplicate.event.id == first.event.id
        assert duplicate.event.episode_id == first.event.episode_id
        assert duplicate.event.participation is not None
        assert duplicate.event.participation.profile_id == "all-devices"
        assert duplicate.event.participation.allowed is True
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_target_snapshot_is_exact_after_profile_and_device_changes(profile_context):
    repo, service = profile_context
    armed = await service.create_profile("Armed", ["camera"])
    await service.activate_profile(armed.id)
    engine = EpisodeEngine(repo, EventBus(), timeout=30, capture_profiles=service)
    await engine.start()
    try:
        result = await engine.ingest_event(
            Event(
                device_id="camera",
                area_id="front",
                event_type="motion_detection",
                source="test",
            )
        )
        assert result.event.eligible_recording_device_ids == ["camera"]
        await repo.upsert_device(
            Device(
                id="camera",
                name="Moved Camera",
                device_type="camera",
                area_id="other",
                enabled=False,
                configs={"video": CapabilityConfig(settings={"recording_mode": "off"})},
            )
        )
        loaded = await repo.get_event(result.event.id)
        assert loaded is not None
        targets = await AreaRecordingTargetResolver(repo).resolve(loaded)
        assert [device.id for device in targets] == ["camera"]
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_profile_filters_area_recording_targets(profile_context):
    repo, service = profile_context
    await repo.upsert_device(
        Device(
            id="overview",
            name="Overview",
            device_type="camera",
            area_id="front",
            configs={
                "video": CapabilityConfig(settings={"recording_mode": "on_episode"}),
            },
        )
    )
    restricted = await service.create_profile("Camera only", ["camera"])
    await service.activate_profile(restricted.id)
    decision, target_ids = await service.evaluate_event(
        Event(
            device_id="camera",
            area_id="front",
            event_type="motion_detection",
            source="test",
        )
    )
    assert decision.allowed is True
    assert target_ids == ["camera"]

    await service.update_profile(restricted.id, "Both cameras", ["camera", "overview"])
    _decision, target_ids = await service.evaluate_event(
        Event(
            device_id="camera",
            area_id="front",
            event_type="motion_detection",
            source="test",
        )
    )
    assert target_ids == ["camera", "overview"]


@pytest.mark.asyncio
async def test_profile_api_crud_activation_and_idempotent_history(profile_context):
    repo, service = profile_context
    app = create_api(
        repo,
        str(repo._data_dir),
        inventory=InventoryService(repo),
        capture_profiles=service,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        initial = await client.get("/api/v1/capture-profiles/active")
        assert initial.status_code == 200
        assert initial.json()["profile"]["id"] == "all-devices"
        created = await client.post(
            "/api/v1/capture-profiles",
            json={"name": "Disarmed", "device_ids": []},
        )
        assert created.status_code == 201
        profile = created.json()
        assert profile["include_all_devices"] is False
        assert profile["active"] is False
        activated = await client.put(
            "/api/v1/capture-profiles/active",
            json={"profile_id": profile["id"]},
        )
        assert activated.status_code == 200
        assert activated.json()["profile"]["id"] == profile["id"]
        changes = activated.json()["recent_changes"]
        assert changes[0]["previous_profile_id"] == "all-devices"
        assert changes[0]["source"] == "api"
        repeated = await client.put(
            "/api/v1/capture-profiles/active",
            json={"profile_id": profile["id"]},
        )
        assert len(repeated.json()["recent_changes"]) == len(changes)
        assert (await client.delete("/api/v1/capture-profiles/all-devices")).status_code == 409
        assert (await client.delete(f"/api/v1/capture-profiles/{profile['id']}")).status_code == 409

        duplicate = await client.post(
            "/api/v1/capture-profiles",
            json={"name": " disARMED ", "device_ids": []},
        )
        assert duplicate.status_code == 409
        unknown_device = await client.post(
            "/api/v1/capture-profiles",
            json={"name": "Invalid", "device_ids": ["missing-device"]},
        )
        assert unknown_device.status_code == 422


@pytest.mark.asyncio
async def test_active_profile_persists_across_repository_restart(tmp_path):
    config = EpisodeConfig(data_dir=str(tmp_path), db_path=str(tmp_path / "episode.db"))
    repo = Repository(config)
    await repo.initialize()
    service = CaptureProfileService(repo)
    profile = await service.create_profile("Disarmed", [])
    await service.activate_profile(profile.id)
    await repo.close()

    restarted = Repository(config)
    await restarted.initialize()
    try:
        active, changes = await CaptureProfileService(restarted).active_response()
        assert active.id == profile.id
        assert active.active is True
        assert changes[0].new_profile_id == profile.id
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_repository_upgrades_beta6_event_schema_at_startup(tmp_path):
    db_path = tmp_path / "episode.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        """CREATE TABLE events (
            id TEXT PRIMARY KEY,
            device_id TEXT NOT NULL,
            area_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            event_type TEXT NOT NULL,
            event_state TEXT NOT NULL,
            source TEXT NOT NULL,
            dedup_key TEXT,
            raw_payload_path TEXT,
            metadata TEXT NOT NULL DEFAULT '{}',
            episode_id TEXT
        )"""
    )
    connection.execute(
        """INSERT INTO events (
            id, device_id, area_id, timestamp, event_type, event_state,
            source, dedup_key, metadata, episode_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "legacy-event",
            "legacy-device",
            "legacy-area",
            "2026-09-09T10:00:00.000000+00:00",
            "motion_detection",
            "active",
            "plugin:test",
            "legacy-dedup-key",
            "{}",
            None,
        ),
    )
    connection.commit()
    connection.close()

    repo = Repository(EpisodeConfig(data_dir=str(tmp_path), db_path=str(db_path)))
    await repo.initialize()
    try:
        columns = {
            row["name"] for row in await repo._conn.execute_fetchall("PRAGMA table_info(events)")
        }
        assert {"participation", "eligible_recording_device_ids"}.issubset(columns)
        event = await repo.get_event("legacy-event")
        assert event is not None
        assert event.participation is None
        assert event.eligible_recording_device_ids is None
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_repository_migrates_and_drops_beta7_capture_filter_columns(tmp_path):
    config = EpisodeConfig(data_dir=str(tmp_path), db_path=str(tmp_path / "episode.db"))
    repo = Repository(config)
    await repo.initialize()
    await repo.upsert_area(Area(id="front", name="Front"))
    for device_id, event_filter in (
        ("enabled", ["security"]),
        ("disabled", ["security"]),
        ("inherit", ["security"]),
    ):
        await repo.upsert_device(
            Device(
                id=device_id,
                name=device_id,
                device_type="camera",
                area_id="front",
                event_filter=event_filter,
            )
        )
    service = CaptureProfileService(repo)
    filtered_profile = await service.create_profile(
        "Filtered", ["enabled"], event_filter=["security"]
    )
    unfiltered_profile = await service.create_profile(
        "Unfiltered", ["disabled"], event_filter=["security"]
    )
    await repo.close()

    # Model Beta.7: its profile boolean and Device tri-state predate event_filter.
    connection = sqlite3.connect(config.db_path)
    connection.execute("ALTER TABLE devices DROP COLUMN event_filter")
    connection.execute("ALTER TABLE capture_profiles DROP COLUMN event_filter")
    connection.execute(
        "ALTER TABLE devices ADD COLUMN generic_event_filter TEXT NOT NULL DEFAULT 'inherit'"
    )
    connection.execute(
        "ALTER TABLE capture_profiles ADD COLUMN filter_generic_events INTEGER NOT NULL DEFAULT 0"
    )
    connection.execute("UPDATE devices SET generic_event_filter = 'enabled' WHERE id = 'enabled'")
    connection.execute("UPDATE devices SET generic_event_filter = 'disabled' WHERE id = 'disabled'")
    connection.execute(
        "UPDATE capture_profiles SET filter_generic_events = 1 WHERE id = ?",
        (filtered_profile.id,),
    )
    connection.commit()
    connection.close()

    upgraded = Repository(config)
    await upgraded.initialize()
    try:
        expected_filter = ["condition", "heartbeat", "motion"]
        enabled = await upgraded.get_device("enabled")
        disabled = await upgraded.get_device("disabled")
        inherit = await upgraded.get_device("inherit")
        assert enabled is not None
        assert enabled.event_filter == expected_filter
        assert disabled is not None
        assert disabled.event_filter == []
        assert inherit is not None
        assert inherit.event_filter is None

        stored_filtered = await upgraded.get_capture_profile(filtered_profile.id)
        stored_unfiltered = await upgraded.get_capture_profile(unfiltered_profile.id)
        assert stored_filtered is not None
        assert stored_filtered.event_filter == expected_filter
        assert stored_unfiltered is not None
        assert stored_unfiltered.event_filter == []

        columns = {
            "devices": {
                row["name"]
                for row in await upgraded._conn.execute_fetchall("PRAGMA table_info(devices)")
            },
            "capture_profiles": {
                row["name"]
                for row in await upgraded._conn.execute_fetchall(
                    "PRAGMA table_info(capture_profiles)"
                )
            },
        }
        assert "event_filter" in columns["devices"]
        assert "event_filter" in columns["capture_profiles"]
        assert "generic_event_filter" not in columns["devices"]
        assert "filter_generic_events" not in columns["capture_profiles"]
    finally:
        await upgraded.close()

    reopened = Repository(config)
    await reopened.initialize()
    try:
        reopened_device = await reopened.get_device("enabled")
        reopened_profile = await reopened.get_capture_profile(filtered_profile.id)
        assert reopened_device is not None
        assert reopened_device.event_filter == ["condition", "heartbeat", "motion"]
        assert reopened_profile is not None
        assert reopened_profile.event_filter == ["condition", "heartbeat", "motion"]
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_repository_rejects_an_unknown_event_schema_at_startup(tmp_path):
    db_path = tmp_path / "episode.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE events (id TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()

    repo = Repository(EpisodeConfig(data_dir=str(tmp_path), db_path=str(db_path)))
    with pytest.raises(RuntimeError, match="unsupported pre-release schema"):
        await repo.initialize()
