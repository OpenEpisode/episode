from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from episode.api.routes import create_api
from episode.api.thumbnails import ThumbnailCache
from episode.config import EpisodeConfig
from episode.domain.models import Area, Device, Episode, EpisodeState, Event, Evidence
from episode.retention import RETENTION_POLICY_SETTING, RetentionService
from episode.storage.repository import Repository


async def _repository_with_inventory(tmp_path):
    repository = Repository(EpisodeConfig(data_dir=str(tmp_path)))
    await repository.initialize()
    await repository.upsert_area(Area(id="driveway", name="Driveway"))
    await repository.upsert_device(
        Device(
            id="camera",
            name="Camera",
            device_type="camera",
            area_id="driveway",
        )
    )
    return repository


@pytest.mark.asyncio
async def test_retention_expires_visual_evidence_and_derivatives_but_not_active_files(
    tmp_path,
):
    repository = await _repository_with_inventory(tmp_path)
    now = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    episode = Episode(
        id="episode-1",
        primary_area_id="driveway",
        start_time=now - timedelta(days=40),
        end_time=now - timedelta(days=39),
        state=EpisodeState.CLOSED,
    )
    await repository.create_episode(episode)

    old_path = tmp_path / "old.jpg"
    recent_path = tmp_path / "recent.jpg"
    active_path = tmp_path / "active.mp4"
    old_path.write_bytes(b"old visual evidence")
    recent_path.write_bytes(b"recent visual evidence")
    active_path.write_bytes(b"active visual evidence")
    old = await repository.create_evidence(
        Evidence(
            id="old-snapshot",
            device_id="camera",
            area_id="driveway",
            timestamp=now - timedelta(days=31),
            evidence_type="snapshot",
            file_path=str(old_path),
            mime_type="image/jpeg",
            original_filename="camera-original.jpg",
            metadata={"origin": "camera", "detection": "human"},
            episode_id=episode.id,
        )
    )
    recent = await repository.create_evidence(
        Evidence(
            id="recent-snapshot",
            device_id="camera",
            area_id="driveway",
            timestamp=now - timedelta(days=29),
            evidence_type="snapshot",
            file_path=str(recent_path),
            mime_type="image/jpeg",
            episode_id=episode.id,
        )
    )
    active = await repository.create_evidence(
        Evidence(
            id="active-recording",
            device_id="camera",
            area_id="driveway",
            timestamp=now - timedelta(days=31),
            evidence_type="recording",
            file_path=str(active_path),
            mime_type="video/mp4",
            episode_id=episode.id,
        )
    )

    thumbnails = ThumbnailCache(tmp_path / "cache" / "thumbnails")
    thumbnail_path = thumbnails._cache_dir / f"{thumbnails._cache_key(old)}.jpg"
    thumbnail_path.parent.mkdir(parents=True)
    thumbnail_path.write_bytes(b"derived thumbnail")
    timelapse = tmp_path / "episodes" / episode.id / "timelapses" / "timelapse.mp4"
    timelapse.parent.mkdir(parents=True, exist_ok=True)
    timelapse.write_bytes(b"derived timelapse")
    active_paths = {str(active_path)}
    retention = RetentionService(
        repository,
        str(tmp_path),
        thumbnails,
        active_paths=lambda: active_paths,
    )
    original_artifact = await repository.get_raw_artifact(old.artifact_id)

    try:
        assert await retention.run_once(now=now) == 1

        expired = await repository.get_evidence(old.id)
        assert expired.availability == "expired"
        assert expired.expiration_reason == "retention_policy"
        assert expired.file_path == ""
        assert expired.original_filename == "camera-original.jpg"
        assert expired.artifact_id == old.artifact_id
        assert expired.byte_size == old.byte_size
        assert expired.sha256 == old.sha256
        assert expired.metadata == old.metadata
        artifact = await repository.get_raw_artifact(old.artifact_id)
        assert artifact.file_path == f"expired:{old.artifact_id}"
        assert artifact.original_filename == "camera-original.jpg"
        assert artifact.byte_size == old.byte_size
        assert artifact.sha256 == old.sha256
        assert {
            key: value
            for key, value in artifact.metadata.items()
            if key not in {"availability", "expired_at", "expiration_reason"}
        } == original_artifact.metadata
        assert artifact.metadata["availability"] == "expired"
        assert not old_path.exists()
        assert not thumbnail_path.exists()
        assert not timelapse.exists()
        assert (await repository.get_evidence(recent.id)).availability == "available"
        assert recent_path.exists()
        assert (await repository.get_evidence(active.id)).availability == "available"
        assert active_path.exists()

        active_paths.clear()
        assert await retention.run_once(now=now) == 1
        assert (await repository.get_evidence(active.id)).availability == "expired"
        assert not active_path.exists()

        manifest_path = tmp_path / "episodes" / episode.id / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_evidence = {item["id"]: item for item in manifest["evidence"]}
        assert manifest_evidence[old.id]["availability"] == "expired"
        assert manifest_evidence[old.id]["file"] is None
        assert manifest_evidence[old.id]["sha256"] == old.sha256
    finally:
        await repository.close()


@pytest.mark.asyncio
async def test_retention_expires_embedded_event_picture_payload(tmp_path):
    repository = await _repository_with_inventory(tmp_path)
    now = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    payload = tmp_path / "event-with-picture.bin"
    payload.write_bytes(b"event header" + b"jpeg bytes")
    event = await repository.create_event(
        Event(
            id="door-event",
            device_id="camera",
            area_id="driveway",
            timestamp=now - timedelta(days=31),
            event_type="door_access",
            source="test",
            raw_payload_path=str(payload),
            metadata={
                "embedded_picture": {
                    "offset": 12,
                    "byte_size": 10,
                    "mime_type": "image/jpeg",
                },
                "picture_sha256": "example",
            },
        )
    )
    retention = RetentionService(
        repository,
        str(tmp_path),
        ThumbnailCache(tmp_path / "cache" / "thumbnails"),
    )

    try:
        assert await retention.run_once(now=now) == 1
        expired = await repository.get_event(event.id)
        assert expired.raw_payload_path is None
        assert "embedded_picture" not in expired.metadata
        assert expired.metadata["visual_evidence"]["availability"] == "expired"
        assert not payload.exists()
    finally:
        await repository.close()


@pytest.mark.asyncio
async def test_retention_retries_failed_file_removal(tmp_path, monkeypatch):
    repository = await _repository_with_inventory(tmp_path)
    now = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    source = tmp_path / "protected.jpg"
    source.write_bytes(b"visual evidence")
    evidence = await repository.create_evidence(
        Evidence(
            id="protected-snapshot",
            device_id="camera",
            area_id="driveway",
            timestamp=now - timedelta(days=31),
            evidence_type="snapshot",
            file_path=str(source),
            mime_type="image/jpeg",
        )
    )
    retention = RetentionService(
        repository,
        str(tmp_path),
        ThumbnailCache(tmp_path / "cache" / "thumbnails"),
    )
    remove_paths = retention._remove_paths

    async def fail_removal(paths):
        raise PermissionError("storage is read-only")

    try:
        monkeypatch.setattr(retention, "_remove_paths", fail_removal)
        assert await retention.run_once(now=now) == 0
        assert (await repository.get_evidence(evidence.id)).availability == "available"
        assert retention.status()["state"] == "degraded"
        assert retention.status()["failure_count"] == 1

        monkeypatch.setattr(retention, "_remove_paths", remove_paths)
        assert await retention.run_once(now=now) == 1
        assert (await repository.get_evidence(evidence.id)).availability == "expired"
        assert retention.status()["state"] == "unavailable"
        assert retention.status()["last_error"] is None
    finally:
        await repository.close()


@pytest.mark.asyncio
async def test_retention_settings_api_updates_policy_and_exposes_status(tmp_path):
    repository = await _repository_with_inventory(tmp_path)
    thumbnails = ThumbnailCache(tmp_path / "cache" / "thumbnails")
    retention = RetentionService(repository, str(tmp_path), thumbnails)
    await retention.start()
    app = create_api(
        repository,
        str(tmp_path),
        thumbnail_cache=thumbnails,
        retention=retention,
    )
    transport = httpx.ASGITransport(app=app)

    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            initial = await client.get("/api/v1/settings/retention")
            updated = await client.put(
                "/api/v1/settings/retention",
                json={"enabled": True, "retention_days": 15},
            )
            disabled = await client.put(
                "/api/v1/settings/retention",
                json={"enabled": False, "retention_days": 15},
            )
            invalid = await client.put(
                "/api/v1/settings/retention",
                json={"enabled": True, "retention_days": 0},
            )

        assert initial.status_code == 200
        assert initial.json()["retention_days"] == 30
        assert initial.json()["enabled"] is True
        assert initial.json()["policy_state"] == "unconfirmed"
        assert initial.json()["confirmed_at"] is None
        assert "requirements vary" in initial.json()["notice"].lower()
        assert updated.status_code == 200
        assert updated.json()["retention_days"] == 15
        assert updated.json()["policy_state"] == "configured"
        assert updated.json()["confirmed_at"] is not None
        assert disabled.status_code == 200
        assert disabled.json()["enabled"] is False
        assert disabled.json()["policy_state"] == "disabled"
        stored = json.loads(await repository.get_system_setting(RETENTION_POLICY_SETTING))
        assert stored["enabled"] is False
        assert stored["retention_days"] == 15
        assert invalid.status_code == 422
    finally:
        await retention.stop()
        await repository.close()


@pytest.mark.asyncio
async def test_retention_start_schedules_initial_cleanup_in_background(tmp_path, monkeypatch):
    repository = await _repository_with_inventory(tmp_path)
    retention = RetentionService(
        repository,
        str(tmp_path),
        ThumbnailCache(tmp_path / "cache" / "thumbnails"),
        interval_seconds=3600,
    )
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def blocked_cleanup(**_kwargs):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return 0

    monkeypatch.setattr(retention, "run_once", blocked_cleanup)

    try:
        await retention.start()
        await asyncio.wait_for(started.wait(), timeout=1)
        assert retention.status()["initial_cleanup"] == "running"
        assert retention.status()["state"] == "healthy"
        assert calls == 1

        release.set()

        async def initial_cleanup_completed():
            while retention.status()["initial_cleanup"] != "completed":
                await asyncio.sleep(0)

        await asyncio.wait_for(initial_cleanup_completed(), timeout=1)
        assert calls == 1
    finally:
        release.set()
        await retention.stop()
        await repository.close()


@pytest.mark.asyncio
async def test_retention_initial_cleanup_failure_is_degraded_and_supervised(tmp_path, monkeypatch):
    repository = await _repository_with_inventory(tmp_path)
    retention = RetentionService(
        repository,
        str(tmp_path),
        ThumbnailCache(tmp_path / "cache" / "thumbnails"),
        interval_seconds=3600,
    )

    async def fail_cleanup(**_kwargs):
        raise RuntimeError("retention storage unavailable")

    monkeypatch.setattr(retention, "run_once", fail_cleanup)

    try:
        await retention.start()

        async def initial_cleanup_failed():
            while retention.status()["initial_cleanup"] != "failed":
                await asyncio.sleep(0)

        await asyncio.wait_for(initial_cleanup_failed(), timeout=1)
        status = retention.status()
        assert status["state"] == "degraded"
        assert status["last_error"] == "retention storage unavailable"
        assert status["failure_count"] == 1
    finally:
        await retention.stop()
        await repository.close()


@pytest.mark.asyncio
async def test_disabled_retention_does_not_expire_visual_evidence(tmp_path):
    repository = await _repository_with_inventory(tmp_path)
    now = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    source = tmp_path / "kept.jpg"
    source.write_bytes(b"kept visual evidence")
    evidence = await repository.create_evidence(
        Evidence(
            id="kept-snapshot",
            device_id="camera",
            area_id="driveway",
            timestamp=now - timedelta(days=365),
            evidence_type="snapshot",
            file_path=str(source),
            mime_type="image/jpeg",
        )
    )
    retention = RetentionService(
        repository,
        str(tmp_path),
        ThumbnailCache(tmp_path / "cache" / "thumbnails"),
    )

    try:
        policy = await retention.set_policy(enabled=False, retention_days=30)
        assert policy.state == "disabled"
        assert await retention.run_once(now=now) == 0
        assert (await repository.get_evidence(evidence.id)).availability == "available"
        assert source.exists()
    finally:
        await repository.close()
