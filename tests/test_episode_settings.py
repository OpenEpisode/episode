from __future__ import annotations

import httpx
import pytest

from episode.api.routes import create_api
from episode.config import EpisodeConfig
from episode.domain.models import Area, Device
from episode.engine.bus import EventBus
from episode.engine.engine import EpisodeEngine
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
async def test_episode_lifecycle_settings_are_persisted_and_bounded(tmp_path):
    repository = await _repository_with_inventory(tmp_path)
    engine = EpisodeEngine(repository, EventBus())
    await engine.start()
    app = create_api(repository, str(tmp_path), engine=engine)
    transport = httpx.ASGITransport(app=app)

    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            initial = await client.get("/api/v1/settings/episode")
            updated = await client.put(
                "/api/v1/settings/episode",
                json={"quiescent_grace_seconds": 12},
            )
            invalid = await client.put(
                "/api/v1/settings/episode",
                json={"quiescent_grace_seconds": 61},
            )

        assert initial.status_code == 200
        assert initial.json()["quiescent_grace_seconds"] == 5
        assert updated.status_code == 200
        assert updated.json()["quiescent_grace_seconds"] == 12
        assert invalid.status_code == 422
        assert await repository.get_system_setting("episode_quiescent_grace_seconds") == "12"

        await engine.stop()
        engine = EpisodeEngine(repository, EventBus())
        await engine.start()
        assert engine.lifecycle_settings()["quiescent_grace_seconds"] == 12
    finally:
        await engine.stop()
        await repository.close()
