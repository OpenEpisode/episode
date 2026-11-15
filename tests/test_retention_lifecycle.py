"""Retention honours the Episode lifecycle and recovers from interruption.

Eligibility is decided at delete time, shared bytes outlive any single reference, and
a crash between removing bytes, committing the tombstone, and journaling it is repaired
on the next start without touching identity or checksum.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from episode.api.thumbnails import ThumbnailCache
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
from episode.retention import RetentionService
from episode.storage.bundles import read_journal
from episode.storage.repository import Repository

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=31)


async def _repository(tmp_path):
    repository = Repository(EpisodeConfig(data_dir=str(tmp_path)))
    await repository.initialize()
    await repository.upsert_area(Area(id="driveway", name="Driveway"))
    await repository.upsert_device(
        Device(id="camera", name="Camera", device_type="camera", area_id="driveway")
    )
    return repository


def _service(repository, tmp_path, **kwargs):
    return RetentionService(
        repository, str(tmp_path), ThumbnailCache(tmp_path / "cache" / "thumbnails"), **kwargs
    )


async def _episode(repository, episode_id, state):
    return await repository.create_episode(
        Episode(
            id=episode_id,
            primary_area_id="driveway",
            start_time=OLD,
            end_time=OLD + timedelta(minutes=5),
            state=state,
        )
    )


async def _snapshot(repository, tmp_path, evidence_id, *, episode_id=None, **overrides):
    path = tmp_path / f"{evidence_id}.jpg"
    path.write_bytes(b"visual " + evidence_id.encode())
    fields = {
        "id": evidence_id,
        "device_id": "camera",
        "area_id": "driveway",
        "timestamp": OLD,
        "evidence_type": "snapshot",
        "file_path": str(path),
        "mime_type": "image/jpeg",
        "episode_id": episode_id,
    }
    fields.update(overrides)
    return await repository.create_evidence(Evidence(**fields)), path


async def _event_picture(repository, tmp_path, event_id, *, episode_id=None, payload=None):
    path = payload or tmp_path / f"{event_id}.bin"
    if payload is None:
        path.write_bytes(b"header" + b"jpeg bytes")
    return await repository.create_event(
        Event(
            id=event_id,
            device_id="camera",
            area_id="driveway",
            timestamp=OLD,
            event_type="door_access",
            source="test",
            raw_payload_path=str(path),
            metadata={
                "embedded_picture": {"offset": 6, "byte_size": 10, "mime_type": "image/jpeg"}
            },
            episode_id=episode_id,
        )
    ), path


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "expires"),
    [
        (EpisodeState.ACTIVE, False),
        (EpisodeState.QUIESCENT, False),
        (EpisodeState.FINALIZING, False),
        (EpisodeState.CLOSED, True),
    ],
)
async def test_only_closed_episodes_expire_their_evidence_and_event_pictures(
    tmp_path, state, expires
):
    repository = await _repository(tmp_path)
    try:
        await _episode(repository, "episode-1", state)
        evidence, evidence_path = await _snapshot(
            repository, tmp_path, "snapshot", episode_id="episode-1"
        )
        event, payload = await _event_picture(repository, tmp_path, "door", episode_id="episode-1")

        assert await _service(repository, tmp_path).run_once(now=NOW) == (2 if expires else 0)

        kept = await repository.get_evidence(evidence.id)
        assert kept.availability == ("expired" if expires else "available")
        assert evidence_path.exists() is not expires
        assert payload.exists() is not expires
        assert (await repository.get_event(event.id)).raw_payload_path == (
            None if expires else str(payload)
        )
    finally:
        await repository.close()


@pytest.mark.asyncio
async def test_a_stale_candidate_is_rechecked_at_delete_time(tmp_path, monkeypatch):
    """The list was valid when built; the Episode is not closed when cleanup acts."""
    repository = await _repository(tmp_path)
    try:
        await _episode(repository, "episode-1", EpisodeState.FINALIZING)
        evidence, path = await _snapshot(repository, tmp_path, "late", episode_id="episode-1")
        stale = [evidence]

        async def stale_candidates(_cutoff):
            return stale

        monkeypatch.setattr(repository, "list_visual_evidence_before", stale_candidates)
        retention = _service(repository, tmp_path)

        assert await retention.run_once(now=NOW) == 0
        assert path.exists()
        assert (await repository.get_evidence(evidence.id)).availability == "available"
        assert retention.status()["failure_count"] == 0
    finally:
        await repository.close()


@pytest.mark.asyncio
async def test_unassigned_visual_material_ages_out_without_an_episode(tmp_path):
    repository = await _repository(tmp_path)
    try:
        evidence, path = await _snapshot(repository, tmp_path, "orphan")
        event, payload = await _event_picture(repository, tmp_path, "orphan-door")

        assert await _service(repository, tmp_path).run_once(now=NOW) == 2
        assert not path.exists() and not payload.exists()
        assert (await repository.get_evidence(evidence.id)).availability == "expired"
        assert (await repository.get_event(event.id)).raw_payload_path is None
    finally:
        await repository.close()


@pytest.mark.asyncio
async def test_a_shared_artifact_keeps_its_bytes_until_the_last_evidence_expires(tmp_path):
    repository = await _repository(tmp_path)
    try:
        await _episode(repository, "closed", EpisodeState.CLOSED)
        await _episode(repository, "open", EpisodeState.ACTIVE)
        first, path = await _snapshot(repository, tmp_path, "first", episode_id="closed")
        second, _ = await _snapshot(
            repository,
            tmp_path,
            "second",
            episode_id="open",
            file_path=str(path),
            artifact_id=first.artifact_id,
            byte_size=first.byte_size,
            sha256=first.sha256,
        )
        retention = _service(repository, tmp_path)

        assert await retention.run_once(now=NOW) == 1
        assert (await repository.get_evidence(first.id)).availability == "expired"
        assert path.exists(), "an un-expired reference still needs these bytes"
        artifact = await repository.get_raw_artifact(first.artifact_id)
        assert artifact.file_path == str(path)
        assert (await repository.get_evidence(second.id)).availability == "available"

        await repository.update_episode_state("open", EpisodeState.CLOSED)
        assert await retention.run_once(now=NOW) == 1
        assert not path.exists()
        assert (await repository.get_raw_artifact(first.artifact_id)).file_path.startswith(
            "expired:"
        )
    finally:
        await repository.close()


@pytest.mark.asyncio
async def test_an_event_picture_sharing_evidence_bytes_survives_the_evidence(tmp_path):
    repository = await _repository(tmp_path)
    try:
        await _episode(repository, "closed", EpisodeState.CLOSED)
        evidence, path = await _snapshot(repository, tmp_path, "shared", episode_id="closed")
        event, _ = await _event_picture(
            repository, tmp_path, "shared-door", episode_id="closed", payload=path
        )
        await repository.create_ingestion_receipt(
            IngestionReceipt(
                source="test",
                artifact_id=evidence.artifact_id,
                device_id="camera",
                area_id="driveway",
                event_id=event.id,
                evidence_id=evidence.id,
            )
        )
        retention = _service(repository, tmp_path)
        original = await repository.get_evidence(evidence.id)

        # The Evidence is the first of the two references to be considered.
        assert await retention._expire_evidence(original, NOW) is True
        assert path.exists(), "the Event's picture still points at these bytes"
        assert (await repository.get_event(event.id)).raw_payload_path == str(path)

        assert await retention._expire_event_picture(event, NOW) is True
        assert not path.exists()
        assert (await repository.get_raw_artifact(evidence.artifact_id)).file_path.startswith(
            "expired:"
        )
    finally:
        await repository.close()


@pytest.mark.asyncio
async def test_failure_after_file_removal_before_the_tombstone_retries_to_the_same_identity(
    tmp_path, monkeypatch
):
    repository = await _repository(tmp_path)
    try:
        await _episode(repository, "closed", EpisodeState.CLOSED)
        evidence, path = await _snapshot(repository, tmp_path, "snap", episode_id="closed")
        retention = _service(repository, tmp_path)
        real = repository.mark_evidence_expired

        async def crash(*args, **kwargs):
            raise OSError("database is locked")

        monkeypatch.setattr(repository, "mark_evidence_expired", crash)
        assert await retention.run_once(now=NOW) == 0
        assert not path.exists()
        assert (await repository.get_evidence(evidence.id)).availability == "available"

        monkeypatch.setattr(repository, "mark_evidence_expired", real)
        assert await _service(repository, tmp_path).run_once(now=NOW) == 1
        tombstone = await repository.get_evidence(evidence.id)
        assert tombstone.availability == "expired"
        assert (tombstone.id, tombstone.sha256, tombstone.byte_size) == (
            evidence.id,
            evidence.sha256,
            evidence.byte_size,
        )
    finally:
        await repository.close()


@pytest.mark.asyncio
async def test_a_journal_append_that_failed_after_the_commit_is_repaired_once_on_restart(
    tmp_path, monkeypatch
):
    repository = await _repository(tmp_path)
    try:
        await _episode(repository, "closed", EpisodeState.CLOSED)
        evidence, _ = await _snapshot(repository, tmp_path, "snap", episode_id="closed")
        event, _ = await _event_picture(repository, tmp_path, "door", episode_id="closed")

        async def no_journal(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(repository, "append_episode_journal", no_journal)
        first = _service(repository, tmp_path)
        assert await first.run_once(now=NOW) == 0
        assert first.status()["failure_count"] == 2
        assert (await repository.get_evidence(evidence.id)).availability == "expired"
        types = {entry["type"] for entry in read_journal(str(tmp_path), "closed")}
        assert "evidence.expired" not in types
        monkeypatch.undo()

        # A new process: nothing in memory, the database and journal are all there is.
        restarted = _service(repository, tmp_path)
        assert await restarted.reconcile() == 2
        assert await restarted.reconcile() == 0

        entries = read_journal(str(tmp_path), "closed")
        expired = [e for e in entries if e["type"] == "evidence.expired"]
        assert [e["data"]["evidence_id"] for e in expired] == [evidence.id]
        assert expired[0]["data"]["sha256"] == evidence.sha256
        assert [
            e["data"]["event_id"] for e in entries if e["type"] == "event.visual_evidence_expired"
        ] == [event.id]
        manifest = json.loads((tmp_path / "episodes" / "closed" / "manifest.json").read_text())
        assert {item["id"]: item["availability"] for item in manifest["evidence"]}[
            evidence.id
        ] == "expired"
    finally:
        await repository.close()


@pytest.mark.asyncio
async def test_cancellation_leaves_no_partial_expiry_and_releases_the_lock(tmp_path, monkeypatch):
    repository = await _repository(tmp_path)
    try:
        await _episode(repository, "closed", EpisodeState.CLOSED)
        evidence, path = await _snapshot(repository, tmp_path, "snap", episode_id="closed")
        retention = _service(repository, tmp_path)

        async def cancelled(*args, **kwargs):
            raise asyncio.CancelledError

        # Cancelled between the tombstone writes and their commit.
        monkeypatch.setattr(repository, "_mark_raw_artifact_expired", cancelled)
        with pytest.raises(asyncio.CancelledError):
            await retention.run_once(now=NOW)
        monkeypatch.undo()

        # An unrelated commit on the shared connection must not publish the half write.
        await repository._conn.commit()
        assert (await repository.get_evidence(evidence.id)).availability == "available"

        assert not path.exists()
        assert await retention.run_once(now=NOW) == 1, "the cleanup lock was released"
        assert (await repository.get_evidence(evidence.id)).availability == "expired"
    finally:
        await repository.close()


@pytest.mark.asyncio
async def test_start_reconciles_before_the_first_sweep(tmp_path, monkeypatch):
    repository = await _repository(tmp_path)
    try:
        retention = _service(repository, tmp_path, interval_seconds=3600)
        order: list[str] = []
        real_run_once = retention.run_once

        async def reconcile():
            order.append("reconcile")
            return 0

        async def run_once(**kwargs):
            order.append("sweep")
            return await real_run_once(**kwargs)

        monkeypatch.setattr(retention, "reconcile", reconcile)
        monkeypatch.setattr(retention, "run_once", run_once)
        await retention.start()
        for _ in range(100):
            if "sweep" in order:
                break
            await asyncio.sleep(0.01)
        await retention.stop()
        assert order[:2] == ["reconcile", "sweep"]
    finally:
        await repository.close()
