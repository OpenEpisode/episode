from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from episode.api.routes import create_api
from episode.config import EpisodeConfig
from episode.domain.models import Area, Device, Evidence
from episode.storage.repository import Repository


async def _repository_with_device(tmp_path):
    repository = Repository(EpisodeConfig(data_dir=str(tmp_path)))
    await repository.initialize()
    await repository.upsert_area(Area(id="driveway", name="Driveway"))
    await repository.upsert_device(
        Device(id="camera", name="Camera", device_type="camera", area_id="driveway")
    )
    return repository


def _evidence(
    evidence_id: str,
    timestamp: datetime,
    *,
    reason: str = "invalid_hls_playlist",
    metadata: dict | None = None,
) -> Evidence:
    return Evidence(
        id=evidence_id,
        device_id="camera",
        area_id="driveway",
        timestamp=timestamp,
        evidence_type="incomplete_recording",
        file_path="/private/episode/index.m3u8",
        mime_type="application/json",
        metadata={
            "reason": reason,
            "fragment_count": 2,
            "playlist_validation": {
                "valid": False,
                "error": "playlist_header_missing",
                "referenced_fragment_count": 0,
                "unreferenced_fragment_count": 1,
                "empty_fragment_count": 0,
                "preserved_temporary_component_count": 1,
                "temporary_components_preserved": True,
                "playlist_temporary_preserved": False,
            },
            "ffmpeg_exit_code": 1,
            **(metadata or {}),
        },
    )


@pytest.mark.asyncio
async def test_finalization_alert_query_filters_expired_and_paginates(tmp_path):
    repository = await _repository_with_device(tmp_path)
    try:
        now = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
        newest = _evidence("newest", now)
        middle = _evidence(
            "middle", now - timedelta(minutes=1), reason="incomplete_hls_finalization"
        )
        expired = _evidence("expired", now - timedelta(minutes=2))
        unrelated = _evidence("unrelated", now - timedelta(minutes=3), reason="startup_recovery")
        complete_type = Evidence(
            id="complete",
            device_id="camera",
            area_id="driveway",
            timestamp=now - timedelta(minutes=4),
            evidence_type="recording",
            file_path="/private/episode/index.m3u8",
            mime_type="application/vnd.apple.mpegurl",
            metadata={"reason": "invalid_hls_playlist"},
        )
        for item in (newest, middle, expired, unrelated, complete_type):
            await repository.create_evidence(item)
        await repository.mark_evidence_expired(expired, expired_at=now, artifact_ids=[])

        page = await repository.list_finalization_alerts(limit=1, offset=1)

        assert [item.id for item in page] == ["middle"]
        assert [item.id for item in await repository.list_finalization_alerts()] == [
            "newest",
            "middle",
        ]
    finally:
        await repository.close()


@pytest.mark.asyncio
async def test_alert_api_projects_safe_diagnostics_without_private_metadata(tmp_path):
    repository = await _repository_with_device(tmp_path)
    try:
        evidence = _evidence(
            "alert-one",
            datetime(2026, 10, 1, 12, tzinfo=timezone.utc),
            metadata={
                "private_path": "/srv/episode/secret",
                "stderr": "credentials=super-secret",
                "ffmpeg_exit_code": "not-an-int",
            },
        )
        evidence.episode_id = None
        await repository.create_evidence(evidence)
        app = create_api(repository, str(tmp_path))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/v1/alerts?limit=50&offset=0")

        assert response.status_code == 200
        assert response.json() == [
            {
                "id": "alert-one",
                "severity": "warning",
                "code": "invalid_hls_playlist",
                "title": "Recording playlist failed validation",
                "message": (
                    "The recording was preserved as incomplete because its HLS playlist "
                    "could not be validated (playlist_header_missing)."
                ),
                "created_at": "2026-10-01T12:00:00Z",
                "device_id": "camera",
                "episode_id": None,
                "evidence_id": "alert-one",
                "playlist_validation": {
                    "valid": False,
                    "error": "playlist_header_missing",
                    "fragment_count": 2,
                    "referenced_fragment_count": 0,
                    "unreferenced_fragment_count": 1,
                    "empty_fragment_count": 0,
                    "preserved_temporary_component_count": 1,
                    "temporary_components_preserved": True,
                    "playlist_temporary_preserved": False,
                },
                "ffmpeg_exit_code": None,
            }
        ]
        assert "/srv/episode/secret" not in response.text
        assert "super-secret" not in response.text
        assert "not-an-int" not in response.text
    finally:
        await repository.close()


@pytest.mark.asyncio
async def test_alert_api_returns_empty_array_without_matching_evidence(tmp_path):
    repository = await _repository_with_device(tmp_path)
    try:
        await repository.create_evidence(
            _evidence(
                "not-an-alert",
                datetime(2026, 10, 1, 12, tzinfo=timezone.utc),
                reason="startup_recovery",
            )
        )
        app = create_api(repository, str(tmp_path))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/v1/alerts")

        assert response.status_code == 200
        assert response.json() == []
    finally:
        await repository.close()
