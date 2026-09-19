"""Time-range bounds for the Event and Evidence APIs.

These cover `observed_from`/`observed_before` query semantics. Event *class*
filtering — suppressing motion, status, tamper, and similar classes from opening
an Episode — is a separate concern and lives in `test_event_filter.py`.
"""

from datetime import datetime, timezone

import httpx
import pytest
import pytest_asyncio

from episode.api.routes import create_api
from episode.config import EpisodeConfig
from episode.domain.models import Area, Device, Event, Evidence
from episode.storage.repository import Repository


@pytest_asyncio.fixture
async def event_filter_api(tmp_path):
    repository = Repository(EpisodeConfig(data_dir=str(tmp_path)))
    await repository.initialize()
    await repository.upsert_area(Area(id="entrance", name="Entrance"))
    await repository.upsert_area(Area(id="garden", name="Garden"))
    await repository.upsert_device(
        Device(id="camera", name="Camera", device_type="camera", area_id="entrance")
    )
    app = create_api(repository, str(tmp_path))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield repository, client
    await repository.close()


def _event(event_id: str, timestamp: datetime, *, area_id: str = "entrance") -> Event:
    return Event(
        id=event_id,
        device_id="camera",
        area_id=area_id,
        timestamp=timestamp,
        event_type="motion_detection",
    )


@pytest.mark.asyncio
async def test_event_time_bounds_are_inclusive_exclusive_and_compose(event_filter_api):
    repository, _client = event_filter_api
    start = datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)
    end = datetime(2026, 9, 14, 11, 0, tzinfo=timezone.utc)
    await repository.create_event(
        _event("before", datetime(2026, 9, 14, 9, 59, tzinfo=timezone.utc))
    )
    await repository.create_event(_event("start", start))
    await repository.create_event(
        _event("inside-later", datetime(2026, 9, 14, 10, 30, tzinfo=timezone.utc))
    )
    await repository.create_event(_event("end", end))
    await repository.create_event(_event("other-area", start.replace(second=1), area_id="garden"))

    events = await repository.list_events(
        area_id="entrance",
        event_type="motion_detection",
        observed_from=start,
        observed_before=end,
    )

    assert [event.id for event in events] == ["inside-later", "start"]

    page = await repository.list_events(
        area_id="entrance",
        observed_from=start,
        observed_before=end,
        limit=1,
        offset=1,
    )
    assert [event.id for event in page] == ["start"]


@pytest.mark.asyncio
async def test_event_api_normalizes_aware_bounds_and_rejects_naive_or_reversed_ranges(
    event_filter_api,
):
    repository, client = event_filter_api
    timestamp = datetime(2026, 9, 14, 9, 30, tzinfo=timezone.utc)
    await repository.create_event(_event("inside", timestamp))

    response = await client.get(
        "/api/v1/events",
        params={
            "area_id": "entrance",
            "observed_from": "2026-09-14T10:00:00+01:00",
            "observed_before": "2026-09-14T11:00:00+01:00",
        },
    )
    assert response.status_code == 200
    assert [event["id"] for event in response.json()] == ["inside"]

    naive = await client.get(
        "/api/v1/events",
        params={"observed_from": "2026-09-14T10:00:00"},
    )
    assert naive.status_code == 422
    assert "observed_from must include a timezone offset" in naive.json()["error"]["message"]

    reversed_range = await client.get(
        "/api/v1/events",
        params={
            "observed_from": "2026-09-14T11:00:00Z",
            "observed_before": "2026-09-14T10:00:00Z",
        },
    )
    assert reversed_range.status_code == 422
    assert (
        "observed_before must be later than observed_from"
        in reversed_range.json()["error"]["message"]
    )


@pytest.mark.asyncio
async def test_evidence_time_bounds_are_inclusive_exclusive_and_compose(event_filter_api):
    repository, _client = event_filter_api
    start = datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)
    end = datetime(2026, 9, 14, 11, 0, tzinfo=timezone.utc)
    await repository.create_evidence(
        Evidence(
            id="before-evidence",
            device_id="camera",
            area_id="entrance",
            timestamp=datetime(2026, 9, 14, 9, 59, tzinfo=timezone.utc),
            evidence_type="snapshot",
        )
    )
    await repository.create_evidence(
        Evidence(
            id="start-evidence",
            device_id="camera",
            area_id="entrance",
            timestamp=start,
            evidence_type="snapshot",
        )
    )
    await repository.create_evidence(
        Evidence(
            id="inside-recording",
            device_id="camera",
            area_id="entrance",
            timestamp=datetime(2026, 9, 14, 10, 30, tzinfo=timezone.utc),
            evidence_type="recording",
        )
    )
    await repository.create_evidence(
        Evidence(
            id="inside-snapshot",
            device_id="camera",
            area_id="entrance",
            timestamp=datetime(2026, 9, 14, 10, 30, 1, tzinfo=timezone.utc),
            evidence_type="snapshot",
        )
    )
    await repository.create_evidence(
        Evidence(
            id="end-evidence",
            device_id="camera",
            area_id="entrance",
            timestamp=end,
            evidence_type="snapshot",
        )
    )

    evidence = await repository.list_evidence(
        area_id="entrance",
        evidence_type="snapshot",
        captured_from=start,
        captured_before=end,
    )
    assert [item.id for item in evidence] == ["inside-snapshot", "start-evidence"]

    page = await repository.list_evidence(
        area_id="entrance",
        evidence_type="snapshot",
        captured_from=start,
        captured_before=end,
        limit=1,
        offset=1,
    )
    assert [item.id for item in page] == ["start-evidence"]

    with pytest.raises(ValueError, match="captured_from must be timezone-aware"):
        await repository.list_evidence(captured_from=datetime(2026, 9, 14, 10, 0))
    with pytest.raises(ValueError, match="captured_before must be later than captured_from"):
        await repository.list_evidence(captured_from=end, captured_before=start)


@pytest.mark.asyncio
async def test_evidence_api_normalizes_aware_bounds_and_rejects_naive_or_reversed_ranges(
    event_filter_api,
):
    repository, client = event_filter_api
    await repository.create_evidence(
        Evidence(
            id="inside-evidence",
            device_id="camera",
            area_id="entrance",
            timestamp=datetime(2026, 9, 14, 9, 30, tzinfo=timezone.utc),
            evidence_type="snapshot",
        )
    )

    response = await client.get(
        "/api/v1/evidence",
        params={
            "area_id": "entrance",
            "captured_from": "2026-09-14T10:00:00+01:00",
            "captured_before": "2026-09-14T11:00:00+01:00",
        },
    )
    assert response.status_code == 200
    assert [evidence["id"] for evidence in response.json()] == ["inside-evidence"]

    naive = await client.get(
        "/api/v1/evidence",
        params={"captured_from": "2026-09-14T10:00:00"},
    )
    assert naive.status_code == 422
    assert "captured_from must include a timezone offset" in naive.json()["error"]["message"]

    reversed_range = await client.get(
        "/api/v1/evidence",
        params={
            "captured_from": "2026-09-14T11:00:00Z",
            "captured_before": "2026-09-14T10:00:00Z",
        },
    )
    assert reversed_range.status_code == 422
    assert (
        "captured_before must be later than captured_from"
        in reversed_range.json()["error"]["message"]
    )
