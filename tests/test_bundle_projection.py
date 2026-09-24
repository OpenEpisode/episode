from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from episode.domain.models import (
    Episode,
    EpisodeState,
    Event,
    EventState,
    Evidence,
    IngestionReceipt,
    ParticipationDecision,
)
from episode.storage.projection import EpisodeBundleProjector


@pytest.mark.asyncio
async def test_manifest_pages_all_episode_rows_in_deterministic_order(tmp_path):
    row_count = 10_001
    episode = Episode(
        id="episode-large",
        primary_area_id="area",
        state=EpisodeState.ACTIVE,
    )
    first_timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    chronological_events = [
        Event(
            id=f"event-{index:05}",
            device_id="camera",
            area_id="area",
            timestamp=first_timestamp + timedelta(seconds=index),
            event_type="motion",
            event_state=EventState.ACTIVE,
            episode_id=episode.id,
        )
        for index in range(row_count)
    ]
    chronological_events[0].participation = ParticipationDecision(
        allowed=True,
        profile_id="all-devices",
        profile_name="All Devices",
        reason="included",
        evaluated_at=first_timestamp,
        activity_window_seconds=45,
    )
    chronological_evidence = [
        Evidence(
            id=f"evidence-{index:05}",
            device_id="camera",
            area_id="area",
            timestamp=first_timestamp + timedelta(seconds=index),
            evidence_type="snapshot",
            episode_id=episode.id,
        )
        for index in range(row_count)
    ]
    chronological_receipts = [
        IngestionReceipt(
            id=f"receipt-{index:05}",
            source="test",
            received_at=first_timestamp + timedelta(seconds=index),
            event_id=chronological_events[index].id,
            episode_id=episode.id,
        )
        for index in range(row_count)
    ]

    class RepositoryStub:
        def __init__(self):
            # Match repository ordering: Events/Evidence newest-first, Receipts
            # oldest-first. Their persistent ID tie-breakers are immaterial
            # here because each generated timestamp is unique.
            self.events = list(reversed(chronological_events))
            self.evidence = list(reversed(chronological_evidence))
            self.receipts = chronological_receipts
            self.event_offsets: list[int] = []
            self.evidence_offsets: list[int] = []
            self.receipt_offsets: list[int] = []

        async def get_episode(self, episode_id):
            assert episode_id == episode.id
            return episode

        async def list_episode_area_snapshots(self, episode_id):
            assert episode_id == episode.id
            return []

        async def list_episode_device_snapshots(self, episode_id):
            assert episode_id == episode.id
            return []

        async def list_events(self, *, episode_id, limit, offset):
            assert episode_id == episode.id
            self.event_offsets.append(offset)
            return self.events[offset : offset + limit]

        async def list_evidence(self, *, episode_id, limit, offset):
            assert episode_id == episode.id
            self.evidence_offsets.append(offset)
            return self.evidence[offset : offset + limit]

        async def list_ingestion_receipts(self, *, episode_id, limit, offset):
            assert episode_id == episode.id
            self.receipt_offsets.append(offset)
            return self.receipts[offset : offset + limit]

        async def get_raw_artifact(self, artifact_id):
            raise AssertionError(f"Unexpected artifact lookup: {artifact_id}")

    repository = RepositoryStub()
    projector = EpisodeBundleProjector(repository, str(tmp_path))

    manifest = await projector._manifest(episode.id)

    assert manifest is not None
    expected_offsets = list(range(0, row_count, 1_000))
    assert repository.event_offsets == expected_offsets
    assert repository.evidence_offsets == expected_offsets
    assert repository.receipt_offsets == expected_offsets

    event_rows = manifest["events"]
    evidence_rows = manifest["evidence"]
    receipt_rows = manifest["receipts"]
    assert len(event_rows) == len(evidence_rows) == len(receipt_rows) == row_count
    assert [row["id"] for row in event_rows] == [item.id for item in chronological_events]
    assert [row["id"] for row in evidence_rows] == [item.id for item in chronological_evidence]
    assert [row["id"] for row in receipt_rows] == [item.id for item in chronological_receipts]
    for rows in (event_rows, evidence_rows, receipt_rows):
        ids = [row["id"] for row in rows]
        assert len(ids) == len(set(ids))
    assert event_rows[0]["receipt_ids"] == ["receipt-00000"]
    assert event_rows[0]["participation"]["activity_window_seconds"] == 45
    assert event_rows[-1]["receipt_ids"] == [f"receipt-{row_count - 1:05}"]
