from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from episode.api.routes import create_api
from episode.config import EpisodeConfig
from episode.domain.models import Area, Device, Episode, EpisodeState
from episode.media.previews import CurrentViewService
from episode.storage.repository import Repository


class FakeRecordings:
    def __init__(self, assignments: dict[str, tuple[str, ...]]) -> None:
        self.assignments = assignments

    def active_device_ids(self, episode_id: str) -> tuple[str, ...]:
        return self.assignments.get(episode_id, ())


class FakeSnapshots:
    def __init__(self, available: set[str]) -> None:
        self.available = available
        self.fetches = 0

    def get(self, device_id: str):
        return (
            SimpleNamespace(snapshot_uri=f"http://camera/{device_id}.jpg")
            if device_id in self.available
            else None
        )

    async def fetch_snapshot(self, device_id: str) -> tuple[bytes, str]:
        self.fetches += 1
        return f"preview:{device_id}".encode(), "image/jpeg"


@pytest.mark.asyncio
async def test_current_views_are_scoped_cached_and_report_unavailable_devices():
    snapshots = FakeSnapshots({"camera-a"})
    recordings = FakeRecordings({"episode-a": ("camera-a", "doorbell")})
    previews = CurrentViewService(snapshots, recordings, refresh_interval_seconds=3)

    assert [(view.device_id, view.mode) for view in previews.describe("episode-a")] == [
        ("camera-a", "snapshot"),
        ("doorbell", "unavailable"),
    ]
    assert await previews.fetch("episode-a", "camera-a") == (
        b"preview:camera-a",
        "image/jpeg",
    )
    assert await previews.fetch("episode-a", "camera-a") == (
        b"preview:camera-a",
        "image/jpeg",
    )
    assert snapshots.fetches == 1

    with pytest.raises(LookupError, match="no current-view provider"):
        await previews.fetch("episode-a", "doorbell")
    with pytest.raises(LookupError, match="not recording"):
        await previews.fetch("other-episode", "camera-a")


@pytest.mark.asyncio
async def test_active_episode_current_view_api_never_exposes_media_credentials(tmp_path):
    repository = Repository(EpisodeConfig(data_dir=str(tmp_path)))
    await repository.initialize()
    await repository.upsert_area(Area(id="front-door", name="Front door"))
    await repository.upsert_device(Device(id="camera-a", name="Entry camera", area_id="front-door"))
    await repository.upsert_device(Device(id="doorbell", name="Doorbell", area_id="front-door"))
    await repository.create_episode(
        Episode(id="episode-a", primary_area_id="front-door", state=EpisodeState.ACTIVE)
    )

    snapshots = FakeSnapshots({"camera-a"})
    previews = CurrentViewService(
        snapshots,
        FakeRecordings({"episode-a": ("camera-a", "doorbell")}),
    )
    app = create_api(repository, str(tmp_path), current_views=previews)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/v1/episodes/episode-a/current-views")
            assert response.status_code == 200
            assert response.json() == [
                {
                    "device_id": "camera-a",
                    "device_name": "Entry camera",
                    "mode": "snapshot",
                    "refresh_interval_seconds": 3,
                    "image_url": "/api/v1/episodes/episode-a/current-views/camera-a",
                    "summary": "Refreshing while this Device records",
                },
                {
                    "device_id": "doorbell",
                    "device_name": "Doorbell",
                    "mode": "unavailable",
                    "refresh_interval_seconds": 3,
                    "image_url": None,
                    "summary": "Recording continues without a preview provider",
                },
            ]
            assert "camera/" not in response.text

            image = await client.get("/api/v1/episodes/episode-a/current-views/camera-a")
            assert image.status_code == 200
            assert image.content == b"preview:camera-a"
            assert image.headers["cache-control"] == "no-store"
    finally:
        await repository.close()
