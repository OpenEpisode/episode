"""Time-range bounds for the Episode collection API."""

from datetime import datetime, timezone

import httpx
import pytest
import pytest_asyncio

from episode.api.routes import create_api
from episode.config import EpisodeConfig
from episode.domain.models import Area, Episode, EpisodeState
from episode.storage.repository import Repository


@pytest_asyncio.fixture
async def episode_time_api(tmp_path):
    repository = Repository(EpisodeConfig(data_dir=str(tmp_path)))
    await repository.initialize()
    await repository.upsert_area(Area(id="entrance", name="Entrance"))
    await repository.upsert_area(Area(id="garden", name="Garden"))
    app = create_api(repository, str(tmp_path))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield repository, client
    await repository.close()


@pytest.mark.asyncio
async def test_episode_time_bounds_are_inclusive_exclusive_and_compose(episode_time_api):
    repository, _client = episode_time_api
    indexes = await repository._conn.execute_fetchall("PRAGMA index_list(episodes)")  # noqa: SLF001
    assert any(row["name"] == "idx_episodes_start_time" for row in indexes)
    start = datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)
    end = datetime(2026, 9, 14, 11, 0, tzinfo=timezone.utc)
    episodes = (
        Episode(
            id="before",
            primary_area_id="entrance",
            start_time=datetime(2026, 9, 14, 9, 59, tzinfo=timezone.utc),
        ),
        Episode(id="start-z", primary_area_id="entrance", start_time=start),
        Episode(
            id="inside",
            primary_area_id="entrance",
            start_time=datetime(2026, 9, 14, 10, 30, tzinfo=timezone.utc),
            state=EpisodeState.CLOSED,
        ),
        Episode(id="start-a", primary_area_id="entrance", start_time=start),
        Episode(id="other-area", primary_area_id="garden", start_time=start),
        Episode(id="end", primary_area_id="entrance", start_time=end),
    )
    for episode in episodes:
        await repository.create_episode(episode)

    filtered = await repository.list_episodes(
        area_id="entrance",
        state=EpisodeState.CLOSED,
        limit=10,
        offset=0,
        started_from=start,
        started_before=end,
    )
    assert [episode.id for episode in filtered] == ["inside"]

    ordered = await repository.list_episodes(
        area_id="entrance",
        started_from=start,
        started_before=end,
    )
    assert [episode.id for episode in ordered] == ["inside", "start-z", "start-a"]

    page = await repository.list_episodes(
        area_id="entrance",
        started_from=start,
        started_before=end,
        limit=1,
        offset=1,
    )
    assert [episode.id for episode in page] == ["start-z"]


@pytest.mark.asyncio
async def test_episode_api_normalizes_aware_bounds_and_rejects_invalid_ranges(episode_time_api):
    repository, client = episode_time_api
    await repository.create_episode(
        Episode(
            id="inside",
            primary_area_id="entrance",
            start_time=datetime(2026, 9, 14, 9, 30, tzinfo=timezone.utc),
        )
    )
    await repository.create_episode(
        Episode(
            id="other-area",
            primary_area_id="garden",
            start_time=datetime(2026, 9, 14, 9, 30, tzinfo=timezone.utc),
        )
    )

    response = await client.get(
        "/api/v1/episodes",
        params={
            "area_id": "entrance",
            "started_from": "2026-09-14T10:00:00+01:00",
            "started_before": "2026-09-14T11:00:00+01:00",
            "limit": 1,
        },
    )
    assert response.status_code == 200
    assert [episode["id"] for episode in response.json()] == ["inside"]

    naive = await client.get(
        "/api/v1/episodes",
        params={"started_from": "2026-09-14T10:00:00"},
    )
    assert naive.status_code == 422
    assert "started_from must include a timezone offset" in naive.json()["error"]["message"]

    reversed_range = await client.get(
        "/api/v1/episodes",
        params={
            "started_from": "2026-09-14T11:00:00Z",
            "started_before": "2026-09-14T10:00:00Z",
        },
    )
    assert reversed_range.status_code == 422
    assert (
        "started_before must be later than started_from"
        in reversed_range.json()["error"]["message"]
    )
