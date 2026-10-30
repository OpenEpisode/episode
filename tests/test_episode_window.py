"""Episode-overlap bounds that let clients load a time window in bulk.

The Timeline needs every Event and Evidence item of every Episode touching a day.
These bounds replace two requests per Episode with three paged collection reads.
"""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio

from episode.api.routes import create_api
from episode.config import EpisodeConfig
from episode.domain.models import Area, Device, Episode, EpisodeState, Event, Evidence
from episode.storage.repository import Repository

DAY = datetime(2026, 9, 14, 0, 0, tzinfo=timezone.utc)
NEXT_DAY = DAY + timedelta(days=1)


@pytest_asyncio.fixture
async def window_api(tmp_path):
    repository = Repository(EpisodeConfig(data_dir=str(tmp_path)))
    await repository.initialize()
    await repository.upsert_area(Area(id="entrance", name="Entrance"))
    await repository.upsert_device(
        Device(id="camera", name="Camera", device_type="camera", area_id="entrance")
    )
    episodes = {
        # Entirely before the day.
        "before": (DAY - timedelta(hours=3), DAY - timedelta(hours=2)),
        # Starts the previous evening and runs into the day.
        "straddles-start": (DAY - timedelta(minutes=30), DAY + timedelta(minutes=30)),
        "inside": (DAY + timedelta(hours=10), DAY + timedelta(hours=10, minutes=5)),
        # Starts exactly at the exclusive upper bound.
        "after": (NEXT_DAY, NEXT_DAY + timedelta(minutes=5)),
    }
    for episode_id, (start, end) in episodes.items():
        await repository.create_episode(
            Episode(
                id=episode_id,
                primary_area_id="entrance",
                start_time=start,
                last_event_time=end,
                end_time=end,
                state=EpisodeState.CLOSED,
            )
        )
        await repository.create_event(
            Event(
                id=f"event-{episode_id}",
                device_id="camera",
                area_id="entrance",
                timestamp=start,
                event_type="motion_detection",
                episode_id=episode_id,
            )
        )
        await repository.create_evidence(
            Evidence(
                id=f"evidence-{episode_id}",
                device_id="camera",
                area_id="entrance",
                timestamp=start,
                evidence_type="recording",
                episode_id=episode_id,
            )
        )
    await repository.create_event(
        Event(
            id="event-unassigned",
            device_id="camera",
            area_id="entrance",
            timestamp=DAY + timedelta(hours=1),
            event_type="motion_detection",
        )
    )
    transport = httpx.ASGITransport(app=create_api(repository, str(tmp_path)))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield repository, client
    await repository.close()


@pytest.mark.asyncio
async def test_window_selects_overlapping_episodes_with_their_events_and_evidence(window_api):
    _repository, client = window_api
    params = {
        "overlaps_from": DAY.isoformat(),
        "overlaps_before": NEXT_DAY.isoformat(),
    }
    episodes = await client.get("/api/v1/episodes", params=params)
    assert {item["id"] for item in episodes.json()} == {"straddles-start", "inside"}

    window = {
        "episode_overlaps_from": DAY.isoformat(),
        "episode_overlaps_before": NEXT_DAY.isoformat(),
    }
    events = await client.get("/api/v1/events", params=window)
    # Events keep their Episode link, and a straddling Episode contributes
    # events observed outside the window; unassigned events never match.
    assert {item["id"]: item["episode_id"] for item in events.json()} == {
        "event-straddles-start": "straddles-start",
        "event-inside": "inside",
    }
    evidence = await client.get("/api/v1/evidence", params=window)
    assert {item["id"] for item in evidence.json()} == {
        "evidence-straddles-start",
        "evidence-inside",
    }

    page = await client.get("/api/v1/events", params={**window, "limit": 1, "offset": 1})
    assert [item["id"] for item in page.json()] == ["event-straddles-start"]


@pytest.mark.asyncio
async def test_window_bounds_are_independent_and_validated(window_api):
    repository, client = window_api
    only_lower = await repository.list_episodes(overlaps_from=NEXT_DAY)
    assert {episode.id for episode in only_lower} == {"after"}
    only_upper = await repository.list_episodes(overlaps_before=DAY)
    assert {episode.id for episode in only_upper} == {"before", "straddles-start"}

    for path, name in (
        ("/api/v1/episodes", "overlaps"),
        ("/api/v1/events", "episode_overlaps"),
        ("/api/v1/evidence", "episode_overlaps"),
    ):
        naive = await client.get(path, params={f"{name}_from": "2026-09-14T00:00:00"})
        assert naive.status_code == 422
        assert f"{name}_from must include a timezone offset" in naive.json()["error"]["message"]
        reversed_range = await client.get(
            path,
            params={f"{name}_from": NEXT_DAY.isoformat(), f"{name}_before": DAY.isoformat()},
        )
        assert reversed_range.status_code == 422
