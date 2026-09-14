from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from episode.config import EpisodeConfig
from episode.domain.models import Area, Device, Episode, EpisodeState, Event, EventState, Evidence
from episode.engine.bus import EventBus
from episode.engine.engine import EpisodeEngine
from episode.storage.repository import Repository


async def _repo_with_inventory(tmp_path) -> Repository:
    repository = Repository(
        EpisodeConfig(data_dir=str(tmp_path), db_path=str(tmp_path / "episode.db"))
    )
    await repository.initialize()
    await repository.upsert_area(Area(id="gate", name="Gate"))
    await repository.upsert_device(
        Device(id="camera", name="Camera", device_type="camera", area_id="gate")
    )
    return repository


@pytest.mark.asyncio
async def test_finalizing_episode_rejects_new_event_and_evidence_associations(tmp_path):
    repository = await _repo_with_inventory(tmp_path)
    engine = EpisodeEngine(repository, EventBus(), timeout=30)
    try:
        original = await engine.ingest_event(
            Event(
                device_id="camera",
                area_id="gate",
                event_type="motion",
                timestamp=datetime.now(tz=timezone.utc),
            )
        )
        episode_id = original.event.episode_id
        await repository.update_episode_state(episode_id, EpisodeState.FINALIZING)

        active = await engine.ingest_event(
            Event(
                device_id="camera",
                area_id="gate",
                event_type="motion",
                timestamp=datetime.now(tz=timezone.utc),
            )
        )
        inactive = await engine.ingest_event(
            Event(
                device_id="camera",
                area_id="gate",
                event_type="motion",
                event_state=EventState.INACTIVE,
                timestamp=datetime.now(tz=timezone.utc),
            )
        )
        evidence = await engine.ingest_evidence(
            Evidence(
                device_id="camera",
                area_id="gate",
                evidence_type="snapshot",
                file_path="",
                episode_id=episode_id,
                timestamp=datetime.now(tz=timezone.utc) - timedelta(hours=1),
            )
        )

        assert active.event.episode_id != episode_id
        assert inactive.event.episode_id != episode_id
        assert evidence.episode_id is None
        finalized_episode = await repository.get_episode(episode_id)
        assert finalized_episode.state == EpisodeState.FINALIZING
        assert finalized_episode.event_count == 1
    finally:
        await repository.close()


@pytest.mark.asyncio
async def test_episode_closes_only_after_finalizer_and_final_manifest(tmp_path):
    repository = await _repo_with_inventory(tmp_path)
    finalized: list[str] = []

    async def finalizer(episode_id: str) -> None:
        assert (await repository.get_episode(episode_id)).state == EpisodeState.FINALIZING
        finalized.append(episode_id)

    engine = EpisodeEngine(repository, EventBus(), timeout=30, finalizer=finalizer)
    episode = Episode(
        id="episode-finalize",
        primary_area_id="gate",
        state=EpisodeState.QUIESCENT,
        start_time=datetime.now(tz=timezone.utc) - timedelta(minutes=1),
        minimum_end_at=datetime.now(tz=timezone.utc) - timedelta(seconds=10),
    )
    await repository.create_episode(episode)
    try:
        await engine._close_timed_out_episodes()
        persisted = await repository.get_episode(episode.id)
        assert finalized == [episode.id]
        assert persisted.state == EpisodeState.CLOSED
        assert persisted.end_time is not None
        assert (
            '"state": "closed"'
            in (tmp_path / "episodes" / episode.id / "manifest.json").read_text()
        )
    finally:
        await repository.close()


@pytest.mark.asyncio
async def test_failed_finalization_remains_retryable_and_idempotent(tmp_path):
    repository = await _repo_with_inventory(tmp_path)
    attempts = 0

    async def finalizer(_episode_id: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("recording did not finalize")

    engine = EpisodeEngine(repository, EventBus(), timeout=30, finalizer=finalizer)
    episode = Episode(
        id="episode-retry",
        primary_area_id="gate",
        state=EpisodeState.FINALIZING,
    )
    await repository.create_episode(episode)
    try:
        await engine._close_timed_out_episodes()
        assert (await repository.get_episode(episode.id)).state == EpisodeState.FINALIZING

        await engine._close_timed_out_episodes()
        assert attempts == 2
        assert (await repository.get_episode(episode.id)).state == EpisodeState.CLOSED

        await engine._close_timed_out_episodes()
        assert attempts == 2
    finally:
        await repository.close()


@pytest.mark.asyncio
async def test_finalizing_episode_completes_after_repository_restart(tmp_path):
    repository = await _repo_with_inventory(tmp_path)
    episode = Episode(id="episode-restart", primary_area_id="gate", state=EpisodeState.FINALIZING)
    await repository.create_episode(episode)
    config = EpisodeConfig(data_dir=str(tmp_path), db_path=str(tmp_path / "episode.db"))
    await repository.close()

    restarted = Repository(config)
    await restarted.initialize()
    calls: list[str] = []

    async def finalizer(episode_id: str) -> None:
        calls.append(episode_id)

    engine = EpisodeEngine(restarted, EventBus(), finalizer=finalizer)
    try:
        await engine.start()
        assert calls == [episode.id]
        assert (await restarted.get_episode(episode.id)).state == EpisodeState.CLOSED
        await engine.stop()
    finally:
        await restarted.close()
