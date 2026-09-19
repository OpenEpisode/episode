from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from episode.config import EpisodeConfig
from episode.domain.models import (
    Area,
    Device,
    Episode,
    EpisodeState,
    Event,
    Evidence,
    IngestionReceipt,
)
from episode.storage import repository as repository_module
from episode.storage.files import describe_artifact
from episode.storage.repository import Repository


async def _repository_with_episode(tmp_path) -> tuple[EpisodeConfig, Repository, Episode]:
    config = EpisodeConfig(
        data_dir=str(tmp_path),
        db_path=str(tmp_path / "episode.db"),
    )
    repository = Repository(config)
    await repository.initialize()
    await repository.upsert_area(Area(id="gate", name="Gate"))
    await repository.upsert_device(
        Device(
            id="gate-camera",
            name="Gate camera",
            device_type="camera",
            area_id="gate",
        )
    )
    episode = Episode(
        id="episode-recovery",
        primary_area_id="gate",
        state=EpisodeState.ACTIVE,
    )
    await repository.create_episode(episode)
    return config, repository, episode


@pytest.mark.asyncio
async def test_restart_reconciles_denormalized_episode_counters(tmp_path):
    config, repository, episode = await _repository_with_episode(tmp_path)
    event = await repository.create_event(
        Event(
            device_id="gate-camera",
            area_id="gate",
            event_type="motion",
            episode_id=episode.id,
        )
    )
    recording = tmp_path / "episodes" / episode.id / "recordings" / "segment.mp4"
    recording.parent.mkdir(parents=True)
    recording.write_bytes(b"recording")
    evidence = await repository.create_evidence(
        Evidence(
            device_id="gate-camera",
            area_id="gate",
            evidence_type="recording",
            file_path=str(recording),
            mime_type="video/mp4",
            episode_id=episode.id,
        )
    )
    stale = await repository.get_episode(episode.id)
    assert stale.event_count == stale.evidence_count == 0
    await repository.close()

    recovered_repository = Repository(config)
    await recovered_repository.initialize()
    try:
        recovered = await recovered_repository.get_episode(episode.id)
        assert recovered.event_count == recovered.evidence_count == 1
        assert [item.id for item in await recovered_repository.list_events(episode.id)] == [
            event.id
        ]
        assert [item.id for item in await recovered_repository.list_evidence(episode.id)] == [
            evidence.id
        ]
    finally:
        await recovered_repository.close()


@pytest.mark.asyncio
async def test_restart_recovers_evidence_moved_before_path_commit(tmp_path, monkeypatch):
    config, repository, episode = await _repository_with_episode(tmp_path)
    original = tmp_path / "orphans" / "snapshots" / "capture.jpg"
    original.write_bytes(b"immutable snapshot")
    evidence = await repository.create_evidence(
        Evidence(
            device_id="gate-camera",
            area_id="gate",
            evidence_type="snapshot",
            file_path=str(original),
            mime_type="image/jpeg",
        )
    )
    artifact_id = evidence.artifact_id
    assert artifact_id

    # Force collision-safe renaming, then simulate a crash before either DB path
    # can be updated.
    target = tmp_path / "episodes" / episode.id / "snapshots"
    target.mkdir(parents=True)
    (target / original.name).write_bytes(b"different existing evidence")
    real_move = repository_module.async_move_to_episode

    async def move_then_crash(*args, **kwargs):
        await real_move(*args, **kwargs)
        raise OSError("simulated crash after move")

    monkeypatch.setattr(repository_module, "async_move_to_episode", move_then_crash)
    with pytest.raises(OSError, match="simulated crash"):
        await repository.add_evidence_to_episode(evidence.id, episode.id)

    associated = await repository.get_evidence(evidence.id)
    assert associated.episode_id == episode.id
    assert associated.file_path == str(original)
    assert not original.exists()
    await repository.close()

    monkeypatch.setattr(repository_module, "async_move_to_episode", real_move)
    recovered_repository = Repository(config)
    await recovered_repository.initialize()
    try:
        recovered = await recovered_repository.get_evidence(evidence.id)
        artifact = await recovered_repository.get_raw_artifact(artifact_id)
        assert recovered is not None
        assert artifact is not None
        assert recovered.file_path == artifact.file_path
        assert recovered.file_path != str(target / original.name)
        assert recovered.file_path.startswith(str(target))
        assert open(recovered.file_path, "rb").read() == b"immutable snapshot"

        manifest = json.loads((tmp_path / "episodes" / episode.id / "manifest.json").read_text())
        assert manifest["evidence"][0]["file"] == (
            f"snapshots/{recovered.file_path.rsplit('/', 1)[-1]}"
        )
    finally:
        await recovered_repository.close()


@pytest.mark.asyncio
async def test_restart_recovers_event_payload_and_receipt_after_interrupted_move(
    tmp_path,
    monkeypatch,
):
    config, repository, episode = await _repository_with_episode(tmp_path)
    observed_at = datetime(2026, 8, 13, 9, 0, tzinfo=timezone.utc)
    original = tmp_path / "orphans" / "events" / "event.xml"
    original.write_bytes(b"<EventNotificationAlert />")
    event = await repository.create_event(
        Event(
            device_id="gate-camera",
            area_id="gate",
            timestamp=observed_at,
            event_type="motion",
            source="test:connector",
            raw_payload_path=str(original),
        )
    )
    artifact, receipt = await repository.persist_delivery(
        describe_artifact(str(original), "event_payload", "application/xml"),
        IngestionReceipt(
            source="test:connector",
            received_at=observed_at,
            observed_at=observed_at,
            device_id="gate-camera",
            area_id="gate",
            event_id=event.id,
        ),
    )
    real_move = repository_module.async_move_to_episode

    async def move_then_crash(*args, **kwargs):
        await real_move(*args, **kwargs)
        raise OSError("simulated crash after move")

    monkeypatch.setattr(repository_module, "async_move_to_episode", move_then_crash)
    with pytest.raises(OSError, match="simulated crash"):
        await repository.add_event_to_episode(event.id, episode.id)

    associated = await repository.get_event(event.id)
    linked_receipt = await repository.get_ingestion_receipt(receipt.id)
    assert associated.episode_id == episode.id
    assert associated.raw_payload_path == str(original)
    assert linked_receipt.episode_id == episode.id
    assert not original.exists()
    await repository.close()

    monkeypatch.setattr(repository_module, "async_move_to_episode", real_move)
    recovered_repository = Repository(config)
    await recovered_repository.initialize()
    try:
        recovered = await recovered_repository.get_event(event.id)
        recovered_artifact = await recovered_repository.get_raw_artifact(artifact.id)
        receipts = await recovered_repository.list_ingestion_receipts(episode_id=episode.id)
        assert recovered is not None
        assert recovered_artifact is not None
        assert recovered.raw_payload_path == recovered_artifact.file_path
        assert recovered.raw_payload_path.startswith(
            str(tmp_path / "episodes" / episode.id / "events")
        )
        assert [item.id for item in receipts] == [receipt.id]

        manifest = json.loads((tmp_path / "episodes" / episode.id / "manifest.json").read_text())
        assert [item["id"] for item in manifest["events"]] == [event.id]
        assert [item["id"] for item in manifest["receipts"]] == [receipt.id]
        assert [item["id"] for item in manifest["artifacts"]] == [artifact.id]
    finally:
        await recovered_repository.close()


@pytest.mark.asyncio
async def test_restart_does_not_inspect_or_rebuild_closed_episode(tmp_path, monkeypatch):
    config, repository, episode = await _repository_with_episode(tmp_path)
    await repository.update_episode_state(episode.id, EpisodeState.CLOSED)
    manifest_path = tmp_path / "episodes" / episode.id / "manifest.json"
    manifest_before = manifest_path.read_bytes()
    mtime_before = manifest_path.stat().st_mtime_ns
    await repository.close()

    async def unexpected_recovery(*_args, **_kwargs):
        raise AssertionError("closed Episode recovery must not run")

    monkeypatch.setattr(repository_module, "reconcile_episode_paths", unexpected_recovery)
    monkeypatch.setattr(repository_module, "reconcile_episode_counts", unexpected_recovery)
    recovered_repository = Repository(config)
    await recovered_repository.initialize()
    try:
        assert manifest_path.read_bytes() == manifest_before
        assert manifest_path.stat().st_mtime_ns == mtime_before
    finally:
        await recovered_repository.close()


@pytest.mark.asyncio
async def test_restart_reconciles_only_unsealed_episode_counters(tmp_path):
    config, repository, active = await _repository_with_episode(tmp_path)
    closed = Episode(
        id="closed-history",
        primary_area_id="gate",
        state=EpisodeState.CLOSED,
    )
    await repository.create_episode(closed)
    for episode in (active, closed):
        await repository.create_event(
            Event(
                id=f"event-{episode.id}",
                device_id="gate-camera",
                area_id="gate",
                event_type="motion",
                episode_id=episode.id,
            )
        )
    await repository.close()

    recovered_repository = Repository(config)
    await recovered_repository.initialize()
    try:
        assert (await recovered_repository.get_episode(active.id)).event_count == 1
        assert (await recovered_repository.get_episode(closed.id)).event_count == 0
    finally:
        await recovered_repository.close()


@pytest.mark.asyncio
async def test_episode_manifest_uses_first_observed_safe_inventory_snapshot(tmp_path, monkeypatch):
    config = EpisodeConfig(data_dir=str(tmp_path), db_path=str(tmp_path / "episode.db"))
    repository = Repository(config)
    await repository.initialize()
    await repository.upsert_area(Area(id="gate", name="Original gate", location="North"))
    await repository.upsert_area(Area(id="yard", name="Yard"))
    await repository.upsert_device(
        Device(
            id="sensor",
            name="Original sensor",
            device_type="sensor",
            area_id="gate",
            ip_address="192.0.2.10",
            username="private-user",
            password="private-password",
            configs={"private-plugin": {"settings": {"token": "private-token"}}},
            metadata={"private-note": "private-metadata"},
        )
    )
    await repository.upsert_device(
        Device(
            id="target-camera",
            name="Original camera",
            device_type="camera",
            area_id="gate",
            ip_address="192.0.2.11",
            password="target-password",
        )
    )
    episode = Episode(
        id="identity-history",
        primary_area_id="gate",
        state=EpisodeState.ACTIVE,
    )
    before_snapshot = datetime.now(tz=timezone.utc)
    await repository.create_episode(episode)
    event = await repository.create_event(
        Event(
            id="identity-event",
            device_id="sensor",
            area_id="gate",
            timestamp=datetime(2020, 1, 1, tzinfo=timezone.utc),
            event_type="tripwire",
            eligible_recording_device_ids=["target-camera"],
        )
    )
    await repository.add_event_to_episode(event.id, episode.id)
    after_snapshot = datetime.now(tz=timezone.utc)

    manifest_path = tmp_path / "episodes" / episode.id / "manifest.json"
    original_manifest = json.loads(manifest_path.read_text())
    assert {item["id"] for item in original_manifest["devices"]} == {
        "sensor",
        "target-camera",
    }
    snapshots = await repository.list_episode_device_snapshots(episode.id)
    assert all(
        before_snapshot <= datetime.fromisoformat(item["recorded_at"]) <= after_snapshot
        for item in snapshots
    )

    await repository.upsert_area(Area(id="gate", name="Renamed gate", location="South"))
    await repository.upsert_device(
        Device(
            id="sensor",
            name="Renamed sensor",
            device_type="sensor",
            area_id="yard",
            ip_address="192.0.2.20",
        )
    )
    await repository.upsert_device(
        Device(
            id="target-camera",
            name="Renamed camera",
            device_type="camera",
            area_id="yard",
            ip_address="192.0.2.21",
        )
    )

    async def inventory_lookup_is_forbidden(*_args, **_kwargs):
        raise AssertionError("portable projection must not read mutable inventory")

    monkeypatch.setattr(repository, "get_area", inventory_lookup_is_forbidden)
    monkeypatch.setattr(repository, "get_device", inventory_lookup_is_forbidden)
    await repository.refresh_episode_manifest(episode.id)
    refreshed_manifest = json.loads(manifest_path.read_text())

    assert refreshed_manifest["areas"] == original_manifest["areas"]
    assert refreshed_manifest["devices"] == original_manifest["devices"]
    serialized = json.dumps(refreshed_manifest)
    for secret in (
        "private-user",
        "private-password",
        "private-token",
        "private-metadata",
        "target-password",
    ):
        assert secret not in serialized
    await repository.close()
