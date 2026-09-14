from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from episode.api.routes import create_api
from episode.api.thumbnails import ThumbnailCache
from episode.config import EpisodeConfig
from episode.domain.models import Area, Device, Episode, EpisodeState, Evidence
from episode.recording.hls import HLSCaptureState, HLSRecordingBundle
from episode.retention import RetentionService
from episode.storage.repository import Repository


def _bundle(tmp_path: Path) -> HLSRecordingBundle:
    state = HLSCaptureState(
        evidence_id="evidence-hls",
        episode_id="episode-hls",
        device_id="camera-hls",
        area_id="area-hls",
        session_id="session-hls",
        started_at=datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc),
    )
    return HLSRecordingBundle.create(
        tmp_path / "episodes" / state.episode_id / "recordings" / state.evidence_id,
        state,
    )


def _write_playable_bundle(bundle: HLSRecordingBundle) -> None:
    (bundle.root / "init.mp4").write_bytes(b"initialization")
    (bundle.root / "segments" / "segment-000000.m4s").write_bytes(b"fragment-a")
    (bundle.root / "segments" / "segment-000001.m4s").write_bytes(b"fragment-b")
    bundle.playlist_path.write_text(
        "#EXTM3U\n"
        "#EXT-X-PROGRAM-DATE-TIME:2026-08-27T12:00:00.000Z\n"
        "#EXTINF:4.0,\nsegments/segment-000000.m4s\n"
        "#EXT-X-PROGRAM-DATE-TIME:2026-08-27T12:00:04.000Z\n"
        "#EXTINF:3.5,\nsegments/segment-000001.m4s\n",
        encoding="utf-8",
    )


def test_component_manifest_inventories_immutable_hls_fragments(tmp_path):
    bundle = _bundle(tmp_path)
    _write_playable_bundle(bundle)

    manifest = bundle.finalize(ended_at=datetime(2026, 8, 27, 12, 0, 8, tzinfo=timezone.utc))

    assert manifest["format"] == "episode.recording-bundle"
    assert manifest["fragment_count"] == 2
    assert not bundle.capture_state_path.exists()
    fragments = {
        item["path"]: item for item in manifest["components"] if item["kind"] == "media_segment"
    }
    first = fragments["segments/segment-000000.m4s"]
    assert first["sequence"] == 0
    assert first["duration_seconds"] == 4.0
    assert first["started_at"] == "2026-08-27T12:00:00.000Z"
    assert first["sha256"] == hashlib.sha256(b"fragment-a").hexdigest()
    assert json.loads(bundle.component_manifest_path.read_text()) == manifest


@pytest.mark.asyncio
async def test_recording_route_serves_only_bundle_components(tmp_path):
    config = EpisodeConfig(data_dir=str(tmp_path))
    repository = Repository(config)
    await repository.initialize()
    await repository.upsert_area(Area(id="area-hls", name="Area"))
    await repository.upsert_device(Device(id="camera-hls", name="Camera", area_id="area-hls"))
    await repository.create_episode(
        Episode(
            id="episode-hls",
            primary_area_id="area-hls",
            state=EpisodeState.CLOSED,
        )
    )
    bundle = _bundle(tmp_path)
    _write_playable_bundle(bundle)
    manifest = bundle.finalize(ended_at=datetime(2026, 8, 27, 12, 0, 8, tzinfo=timezone.utc))
    evidence = Evidence(
        id="evidence-hls",
        device_id="camera-hls",
        area_id="area-hls",
        timestamp=bundle.state.started_at,
        evidence_type="recording",
        file_path=str(bundle.playlist_path),
        mime_type="application/vnd.apple.mpegurl",
        episode_id="episode-hls",
        metadata={
            "format": "hls-fmp4",
            "started_at": bundle.state.started_at.isoformat(),
            "recording_session_id": "session-hls",
            "bundle_bytes": manifest["total_bytes"],
            "component_manifest_sha256": bundle.component_manifest_sha256(),
        },
    )
    await repository.create_evidence(evidence)
    app = create_api(repository, str(tmp_path))
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            entrypoint = await client.get(
                "/api/v1/evidence/evidence-hls/file",
                follow_redirects=False,
            )
            playlist = await client.get("/api/v1/recordings/evidence-hls/index.m3u8")
            fragment = await client.get(
                "/api/v1/recordings/evidence-hls/segments/segment-000000.m4s"
            )
            capture_state = await client.get("/api/v1/recordings/evidence-hls/capture.json")
        assert entrypoint.status_code == 307
        assert entrypoint.headers["location"] == ("/api/v1/recordings/evidence-hls/index.m3u8")
        assert playlist.status_code == 200
        assert playlist.headers["cache-control"] == "no-store"
        assert fragment.content == b"fragment-a"
        assert "immutable" in fragment.headers["cache-control"]
        assert capture_state.status_code == 404
    finally:
        await repository.close()


@pytest.mark.asyncio
async def test_startup_reconciliation_keeps_hls_entrypoint_inside_bundle(tmp_path):
    config = EpisodeConfig(data_dir=str(tmp_path))
    repository = Repository(config)
    await repository.initialize()
    await repository.upsert_area(Area(id="area-hls", name="Area"))
    await repository.upsert_device(Device(id="camera-hls", name="Camera", area_id="area-hls"))
    await repository.create_episode(
        Episode(
            id="episode-hls",
            primary_area_id="area-hls",
            state=EpisodeState.FINALIZING,
        )
    )
    bundle = _bundle(tmp_path)
    _write_playable_bundle(bundle)
    manifest = bundle.finalize(ended_at=datetime(2026, 8, 27, 12, 0, 8, tzinfo=timezone.utc))
    evidence = Evidence(
        id="evidence-hls",
        device_id="camera-hls",
        area_id="area-hls",
        timestamp=bundle.state.started_at,
        evidence_type="recording",
        file_path=str(bundle.playlist_path),
        mime_type="application/vnd.apple.mpegurl",
        episode_id="episode-hls",
        metadata={
            "format": "hls-fmp4",
            "started_at": bundle.state.started_at.isoformat(),
            "recording_session_id": "session-hls",
            "bundle_bytes": manifest["total_bytes"],
            "component_manifest_sha256": bundle.component_manifest_sha256(),
        },
    )
    await repository.create_evidence(evidence)
    await repository.close()

    restarted = Repository(config)
    await restarted.initialize()
    try:
        recovered = await restarted.get_evidence(evidence.id)
        assert recovered.file_path == str(bundle.playlist_path)
        assert bundle.playlist_path.exists()
        assert (bundle.root / "segments" / "segment-000000.m4s").exists()
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_startup_reconciliation_repairs_flattened_hls_entrypoint(tmp_path):
    config = EpisodeConfig(data_dir=str(tmp_path))
    repository = Repository(config)
    await repository.initialize()
    await repository.upsert_area(Area(id="area-hls", name="Area"))
    await repository.upsert_device(Device(id="camera-hls", name="Camera", area_id="area-hls"))
    await repository.create_episode(
        Episode(
            id="episode-hls",
            primary_area_id="area-hls",
            state=EpisodeState.FINALIZING,
        )
    )
    bundle = _bundle(tmp_path)
    _write_playable_bundle(bundle)
    manifest = bundle.finalize(ended_at=datetime(2026, 8, 27, 12, 0, 8, tzinfo=timezone.utc))
    evidence = Evidence(
        id="evidence-hls",
        device_id="camera-hls",
        area_id="area-hls",
        timestamp=bundle.state.started_at,
        evidence_type="recording",
        file_path=str(bundle.playlist_path),
        mime_type="application/vnd.apple.mpegurl",
        episode_id="episode-hls",
        metadata={
            "format": "hls-fmp4",
            "started_at": bundle.state.started_at.isoformat(),
            "recording_session_id": "session-hls",
            "bundle_bytes": manifest["total_bytes"],
            "component_manifest_sha256": bundle.component_manifest_sha256(),
        },
    )
    await repository.create_evidence(evidence)
    flattened = bundle.root.parent / "index.m3u8"
    bundle.playlist_path.replace(flattened)
    await repository._conn.execute(
        "UPDATE evidence SET file_path = ? WHERE id = ?",
        (str(flattened), evidence.id),
    )
    await repository._provenance.update_artifact_path(evidence.artifact_id, str(flattened))
    await repository._conn.commit()
    await repository.close()

    restarted = Repository(config)
    await restarted.initialize()
    try:
        recovered = await restarted.get_evidence(evidence.id)
        assert recovered.file_path == str(bundle.playlist_path)
        assert bundle.playlist_path.exists()
        assert not flattened.exists()

        app = create_api(restarted, str(tmp_path))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            fragment = await client.get(
                "/api/v1/recordings/evidence-hls/segments/segment-000000.m4s"
            )
            component_manifest = await client.get("/api/v1/recordings/evidence-hls/manifest.json")
        assert fragment.content == b"fragment-a"
        assert component_manifest.status_code == 200
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_linking_incomplete_hls_evidence_keeps_whole_bundle_in_recordings(
    tmp_path,
):
    repository = Repository(EpisodeConfig(data_dir=str(tmp_path)))
    await repository.initialize()
    await repository.upsert_area(Area(id="area-hls", name="Area"))
    await repository.upsert_device(Device(id="camera-hls", name="Camera", area_id="area-hls"))
    await repository.create_episode(
        Episode(
            id="episode-hls",
            primary_area_id="area-hls",
            state=EpisodeState.ACTIVE,
        )
    )
    bundle = _bundle(tmp_path)
    _write_playable_bundle(bundle)
    manifest = bundle.finalize(ended_at=datetime(2026, 8, 27, 12, 0, 8, tzinfo=timezone.utc))
    evidence = Evidence(
        id="evidence-hls",
        device_id="camera-hls",
        area_id="area-hls",
        timestamp=bundle.state.started_at,
        evidence_type="incomplete_recording",
        file_path=str(bundle.playlist_path),
        mime_type="application/vnd.apple.mpegurl",
        metadata={
            "format": "hls-fmp4",
            "started_at": bundle.state.started_at.isoformat(),
            "recording_session_id": "session-hls",
            "bundle_bytes": manifest["total_bytes"],
            "component_manifest_sha256": bundle.component_manifest_sha256(),
        },
    )
    try:
        await repository.create_evidence(evidence)
        await repository.add_evidence_to_episode(evidence.id, "episode-hls")
        linked = await repository.get_evidence(evidence.id)
        assert linked.file_path == str(bundle.playlist_path)
        assert bundle.component_manifest_path.exists()
        assert (bundle.root / "segments" / "segment-000000.m4s").exists()
    finally:
        await repository.close()


@pytest.mark.asyncio
async def test_retention_removes_complete_hls_bundle_and_keeps_tombstone(tmp_path):
    config = EpisodeConfig(data_dir=str(tmp_path))
    repository = Repository(config)
    await repository.initialize()
    await repository.upsert_area(Area(id="area-hls", name="Area"))
    await repository.upsert_device(Device(id="camera-hls", name="Camera", area_id="area-hls"))
    await repository.create_episode(
        Episode(
            id="episode-hls",
            primary_area_id="area-hls",
            state=EpisodeState.CLOSED,
        )
    )
    bundle = _bundle(tmp_path)
    _write_playable_bundle(bundle)
    ended_at = datetime.now(tz=timezone.utc) - timedelta(days=40)
    manifest = bundle.finalize(ended_at=ended_at)
    evidence = Evidence(
        id="evidence-hls",
        device_id="camera-hls",
        area_id="area-hls",
        timestamp=ended_at,
        evidence_type="recording",
        file_path=str(bundle.playlist_path),
        mime_type="application/vnd.apple.mpegurl",
        episode_id="episode-hls",
        metadata={
            "format": "hls-fmp4",
            "started_at": bundle.state.started_at.isoformat(),
            "recording_session_id": "session-hls",
            "fragment_count": 2,
            "bundle_bytes": manifest["total_bytes"],
            "component_manifest_sha256": bundle.component_manifest_sha256(),
        },
    )
    await repository.create_evidence(evidence)
    retention = RetentionService(
        repository,
        str(tmp_path),
        ThumbnailCache(tmp_path / "cache"),
    )
    await retention.set_policy(enabled=True, retention_days=30)

    assert not bundle.root.exists()
    retained = await repository.get_evidence(evidence.id)
    assert retained.availability == "expired"
    assert retained.metadata["fragment_count"] == 2
    assert retained.metadata["component_manifest_sha256"]
    await repository.close()
