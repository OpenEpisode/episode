from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from episode.capture_profiles import CaptureProfileService
from episode.config import EpisodeConfig
from episode.domain.event_filter import (
    ATTACHMENT_ATTACHED,
    ATTACHMENT_NO_OPEN_EPISODE,
    FILTERED_REASON,
)
from episode.domain.models import (
    Area,
    CapabilityConfig,
    Device,
    EpisodeState,
    Event,
    EventState,
    IngestionReceipt,
    ParticipationDecision,
    RawArtifact,
)
from episode.engine.bus import EventBus
from episode.engine.engine import CanonicalEventResult, EpisodeEngine
from episode.storage.files import describe_artifact, save_bytes
from episode.storage.repository import Repository


def _config(tmp_path) -> EpisodeConfig:
    return EpisodeConfig(data_dir=str(tmp_path), db_path=str(tmp_path / "episode.db"))


async def _ready_repository(config: EpisodeConfig, *, activity_window: int = 45):
    repo = Repository(config)
    await repo.initialize()
    await repo.upsert_area(Area(id="front", name="Front"))
    await repo.upsert_device(
        Device(
            id="camera",
            name="Camera",
            device_type="camera",
            area_id="front",
            activity_window_seconds=activity_window,
            configs={
                "video": CapabilityConfig(
                    protocol="rtsp",
                    port=554,
                    path="/stream",
                    settings={"recording_mode": "on_event"},
                )
            },
        )
    )
    return repo


async def _raw_receipt(
    repo: Repository,
    data_dir: str,
    received_at: datetime,
) -> tuple[RawArtifact, IngestionReceipt]:
    path = save_bytes(data_dir, "orphans/ingress/test", b"preserved delivery", prefix="raw")
    artifact = describe_artifact(path, "event_payload", "application/octet-stream")
    receipt = IngestionReceipt(source="test", received_at=received_at)
    return await repo.persist_delivery(artifact, receipt)


def _event(*, timestamp: datetime, raw_payload_path: str | None = None) -> Event:
    return Event(
        device_id="camera",
        area_id="front",
        timestamp=timestamp,
        event_type="motion_detection",
        event_state=EventState.ACTIVE,
        source="test",
        raw_payload_path=raw_payload_path,
    )


async def _disarm(repo: Repository, service: CaptureProfileService) -> None:
    disarmed = await service.create_profile("Disarmed", [])
    await service.activate_profile(disarmed.id, source="test")


@pytest.mark.asyncio
async def test_duplicate_redelivery_recovers_orphan_from_stored_decision(tmp_path, monkeypatch):
    config = _config(tmp_path)
    repo = await _ready_repository(config)
    service = CaptureProfileService(repo)
    bus = EventBus()
    engine = EpisodeEngine(repo, bus, timeout=30, capture_profiles=service)
    await engine.start()
    dispatched: list[CanonicalEventResult] = []
    bus.subscribe(
        "event.canonicalized",
        lambda msg: dispatched.append(msg.data["result"]),
    )
    original_correlate = engine._correlate
    crashed = False

    async def crash_before_correlation(*args, **kwargs):
        nonlocal crashed
        if not crashed:
            crashed = True
            raise RuntimeError("simulated crash after canonical commit")
        await original_correlate(*args, **kwargs)

    monkeypatch.setattr(engine, "_correlate", crash_before_correlation)
    received_at = datetime.now(timezone.utc) - timedelta(seconds=3)
    first_receipt = IngestionReceipt(source="first", received_at=received_at)
    await repo.create_ingestion_receipt(first_receipt)
    candidate = _event(timestamp=received_at - timedelta(seconds=2))
    try:
        with pytest.raises(RuntimeError, match="simulated crash"):
            await engine.ingest_event(candidate, receipt=first_receipt)

        orphan = await repo.find_event_by_dedup_key(candidate.dedup_key)
        assert orphan is not None and orphan.episode_id is None
        assert orphan.participation is not None
        assert orphan.participation.activity_window_seconds == 45
        assert orphan.eligible_recording_device_ids == ["camera"]

        # Both the profile and current Device window changed after the durable
        # decision. Duplicate recovery must consume the persisted snapshot.
        await _disarm(repo, service)
        device = await repo.get_device("camera")
        device.activity_window_seconds = 1
        await repo.upsert_device(device)

        duplicate_receipt = IngestionReceipt(
            source="duplicate",
            received_at=datetime.now(timezone.utc),
        )
        await repo.create_ingestion_receipt(duplicate_receipt)
        duplicate = _event(timestamp=candidate.timestamp)
        duplicate.dedup_key = candidate.dedup_key
        result = await engine.ingest_event(duplicate, receipt=duplicate_receipt)

        assert result.created is False
        assert result.event.episode_id is not None
        episode = await repo.get_episode(result.event.episode_id)
        assert episode is not None
        assert episode.last_activity_at == received_at
        assert episode.minimum_end_at == received_at + timedelta(seconds=45)
        assert result.event.participation.allowed is True
        assert result.event.eligible_recording_device_ids == ["camera"]
        assert len(dispatched) == 1
        assert dispatched[0].event.id == result.event.id
        assert dispatched[0].created is True
    finally:
        await engine.stop()
        await repo.close()


@pytest.mark.asyncio
async def test_restart_backfills_receipt_after_canonical_commit_before_link(tmp_path, monkeypatch):
    config = _config(tmp_path)
    repo = await _ready_repository(config)
    service = CaptureProfileService(repo)
    engine = EpisodeEngine(repo, EventBus(), timeout=30, capture_profiles=service)
    await engine.start()
    received_at = datetime.now(timezone.utc) - timedelta(seconds=4)
    artifact, receipt = await _raw_receipt(repo, config.data_dir, received_at)
    original_link = repo.link_ingestion_receipt

    async def crash_before_link(receipt_id, *, event_id=None, evidence_id=None, episode_id=None):
        if event_id is not None:
            raise RuntimeError("simulated crash before receipt link")
        await original_link(
            receipt_id,
            event_id=event_id,
            evidence_id=evidence_id,
            episode_id=episode_id,
        )

    monkeypatch.setattr(repo, "link_ingestion_receipt", crash_before_link)
    candidate = _event(
        timestamp=received_at - timedelta(minutes=2),
        raw_payload_path=artifact.file_path,
    )
    try:
        with pytest.raises(RuntimeError, match="before receipt link"):
            await engine.ingest_event(candidate, receipt=receipt)
        persisted = await repo.find_event_by_dedup_key(candidate.dedup_key)
        assert persisted is not None and persisted.episode_id is None
        stored_receipt = await repo.get_ingestion_receipt(receipt.id)
        assert stored_receipt is not None and stored_receipt.event_id is None
    finally:
        await engine.stop()
        await repo.close()

    # Restart under a now-disarmed profile and changed Device window. The exact
    # Raw Artifact path repairs provenance before the orphan is correlated.
    repo = await _ready_repository(config, activity_window=1)
    service = CaptureProfileService(repo)
    await _disarm(repo, service)
    restarted = EpisodeEngine(repo, EventBus(), timeout=30, capture_profiles=service)
    try:
        await restarted.start()
        recovered = await repo.find_event_by_dedup_key(candidate.dedup_key)
        assert recovered is not None and recovered.episode_id is not None
        assert recovered.participation.activity_window_seconds == 45
        assert recovered.eligible_recording_device_ids == ["camera"]
        episode = await repo.get_episode(recovered.episode_id)
        assert episode is not None and episode.state == EpisodeState.ACTIVE
        assert episode.last_activity_at == received_at
        assert episode.minimum_end_at == received_at + timedelta(seconds=45)

        stored_receipt = await repo.get_ingestion_receipt(receipt.id)
        stored_artifact = await repo.get_raw_artifact(artifact.id)
        assert stored_receipt is not None
        assert stored_receipt.event_id == recovered.id
        assert stored_receipt.episode_id == episode.id
        assert stored_artifact is not None
        assert stored_artifact.file_path.startswith(str(tmp_path / "episodes" / episode.id))
        with open(stored_artifact.file_path, "rb") as raw_file:
            assert raw_file.read() == b"preserved delivery"
    finally:
        await restarted.stop()
        await repo.close()


@pytest.mark.asyncio
async def test_beta_8_decision_without_activity_window_uses_compatibility_fallback(tmp_path):
    config = _config(tmp_path)
    repo = await _ready_repository(config, activity_window=73)
    service = CaptureProfileService(repo)
    received_at = datetime.now(timezone.utc) - timedelta(seconds=3)
    candidate = _event(timestamp=received_at - timedelta(minutes=1))
    candidate.participation = ParticipationDecision(
        allowed=True,
        profile_id="stored-profile",
        profile_name="Stored profile",
        reason="all_devices_profile",
        evaluated_at=received_at,
    )
    candidate.eligible_recording_device_ids = ["camera"]
    candidate = await repo.create_event(candidate)
    receipt = IngestionReceipt(source="beta.8", received_at=received_at)
    await repo.create_ingestion_receipt(receipt)
    await repo.link_ingestion_receipt(receipt.id, event_id=candidate.id)
    # Beta.8 serialized ParticipationDecision before this optional key existed.
    await repo._conn.execute(
        "UPDATE events SET participation = json_remove(participation, '$.activity_window_seconds') "
        "WHERE id = ?",
        (candidate.id,),
    )
    await repo._conn.commit()

    await _disarm(repo, service)
    device = await repo.get_device("camera")
    device.activity_window_seconds = 91
    await repo.upsert_device(device)
    engine = EpisodeEngine(repo, EventBus(), timeout=30, capture_profiles=service)
    try:
        await engine.start()
        recovered = await repo.get_event(candidate.id)
        assert recovered is not None and recovered.episode_id is not None
        assert recovered.participation.allowed is True
        assert recovered.participation.activity_window_seconds is None
        assert recovered.eligible_recording_device_ids == ["camera"]
        episode = await repo.get_episode(recovered.episode_id)
        assert episode is not None
        assert episode.minimum_end_at == received_at + timedelta(seconds=91)
    finally:
        await engine.stop()
        await repo.close()


@pytest.mark.asyncio
async def test_expired_duplicate_recovery_is_finalizable_without_stale_actions(
    tmp_path, monkeypatch
):
    config = _config(tmp_path)
    repo = await _ready_repository(config, activity_window=2)
    service = CaptureProfileService(repo)
    bus = EventBus()
    engine = EpisodeEngine(repo, bus, timeout=30, capture_profiles=service)
    await engine.start(defer_finalization=True)
    episode_created: list[dict] = []
    canonicalized: list[CanonicalEventResult] = []
    bus.subscribe("episode.created", lambda msg: episode_created.append(msg.data))
    bus.subscribe(
        "event.canonicalized",
        lambda msg: canonicalized.append(msg.data["result"]),
    )
    original_correlate = engine._correlate
    crashed = False

    async def crash_before_correlation(*args, **kwargs):
        nonlocal crashed
        if not crashed:
            crashed = True
            raise RuntimeError("simulated crash after canonical commit")
        await original_correlate(*args, **kwargs)

    monkeypatch.setattr(engine, "_correlate", crash_before_correlation)
    expired_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    receipt = IngestionReceipt(source="first", received_at=expired_at)
    await repo.create_ingestion_receipt(receipt)
    candidate = _event(timestamp=expired_at)
    try:
        with pytest.raises(RuntimeError, match="simulated crash"):
            await engine.ingest_event(candidate, receipt=receipt)

        duplicate_receipt = IngestionReceipt(
            source="duplicate",
            received_at=datetime.now(timezone.utc),
        )
        await repo.create_ingestion_receipt(duplicate_receipt)
        duplicate = _event(timestamp=candidate.timestamp)
        duplicate.dedup_key = candidate.dedup_key
        result = await engine.ingest_event(duplicate, receipt=duplicate_receipt)

        assert result.created is False
        assert result.event.episode_id is not None
        assert episode_created == []
        assert canonicalized and all(not item.created for item in canonicalized)
        await engine._close_timed_out_episodes()
        historical = await repo.get_episode(result.event.episode_id)
        assert historical is not None and historical.state == EpisodeState.FINALIZING
    finally:
        await engine.stop()
        await repo.close()


@pytest.mark.asyncio
async def test_duplicate_recovery_reuses_episode_created_before_event_link(tmp_path, monkeypatch):
    config = _config(tmp_path)
    repo = await _ready_repository(config)
    engine = EpisodeEngine(repo, EventBus(), timeout=30)
    await engine.start()
    original_add = repo.add_event_to_episode
    candidate = _event(timestamp=datetime.now(timezone.utc))
    received_at = datetime.now(timezone.utc) - timedelta(seconds=2)
    first_receipt = IngestionReceipt(source="first", received_at=received_at)
    await repo.create_ingestion_receipt(first_receipt)
    failed = False

    async def crash_before_event_link(event_id, episode_id, **kwargs):
        nonlocal failed
        if event_id == candidate.id and not failed:
            failed = True
            raise RuntimeError("simulated crash after Episode commit")
        await original_add(event_id, episode_id, **kwargs)

    monkeypatch.setattr(repo, "add_event_to_episode", crash_before_event_link)
    try:
        with pytest.raises(RuntimeError, match="after Episode commit"):
            await engine.ingest_event(candidate, receipt=first_receipt)

        orphan = await repo.find_event_by_dedup_key(candidate.dedup_key)
        assert orphan is not None and orphan.episode_id is None
        shells = await repo.list_episodes()
        assert len(shells) == 1 and shells[0].event_count == 0

        monkeypatch.setattr(repo, "add_event_to_episode", original_add)
        duplicate_receipt = IngestionReceipt(
            source="duplicate",
            received_at=datetime.now(timezone.utc),
        )
        await repo.create_ingestion_receipt(duplicate_receipt)
        duplicate = _event(timestamp=candidate.timestamp)
        duplicate.dedup_key = candidate.dedup_key
        recovered = await engine.ingest_event(duplicate, receipt=duplicate_receipt)

        assert recovered.created is False
        assert recovered.event.episode_id == shells[0].id
        episode = await repo.get_episode(shells[0].id)
        assert episode is not None and episode.event_count == 1
        assert await repo.list_episodes() == [episode]
    finally:
        await engine.stop()
        await repo.close()


@pytest.mark.asyncio
async def test_recovery_uses_ingress_bounds_not_source_clock_for_open_episodes(tmp_path):
    config = _config(tmp_path)
    repo = await _ready_repository(config, activity_window=600)
    engine = EpisodeEngine(repo, EventBus(), timeout=30)
    await engine.start()
    try:
        now = datetime.now(timezone.utc)
        anchor_ingress = now - timedelta(seconds=10)
        anchor_receipt = IngestionReceipt(source="anchor", received_at=anchor_ingress)
        await repo.create_ingestion_receipt(anchor_receipt)
        # The camera clock is far behind the actual receipt time.
        anchor = await engine.ingest_event(
            _event(timestamp=now - timedelta(minutes=30)),
            receipt=anchor_receipt,
        )
        anchor_episode_id = anchor.event.episode_id
        assert anchor_episode_id is not None

        def orphan_at(ingress_time: datetime, event_time: datetime) -> Event:
            event = _event(timestamp=event_time)
            event.participation = ParticipationDecision(
                allowed=True,
                profile_id="stored-profile",
                profile_name="Stored profile",
                reason="all_devices_profile",
                evaluated_at=ingress_time,
                activity_window_seconds=600,
            )
            event.eligible_recording_device_ids = ["camera"]
            return event

        earlier_ingress = anchor_ingress - timedelta(seconds=5)
        earlier = orphan_at(earlier_ingress, now - timedelta(minutes=60))
        earlier_receipt = IngestionReceipt(source="earlier", received_at=earlier_ingress)
        await repo.create_ingestion_receipt(earlier_receipt)
        earlier = await repo.create_event(earlier)
        await repo.link_ingestion_receipt(earlier_receipt.id, event_id=earlier.id)

        later_ingress = anchor_ingress + timedelta(seconds=5)
        # This camera timestamp is ahead of ingress; recovery must still join
        # the Episode that had already received an allowed Event by this time.
        later = orphan_at(later_ingress, now + timedelta(minutes=20))
        later_receipt = IngestionReceipt(source="later", received_at=later_ingress)
        await repo.create_ingestion_receipt(later_receipt)
        later = await repo.create_event(later)
        await repo.link_ingestion_receipt(later_receipt.id, event_id=later.id)

        await engine._recover_unassigned_active_events()

        recovered_earlier = await repo.get_event(earlier.id)
        recovered_later = await repo.get_event(later.id)
        assert recovered_earlier is not None and recovered_earlier.episode_id is not None
        assert recovered_earlier.episode_id != anchor_episode_id
        assert recovered_later is not None
        assert recovered_later.episode_id == anchor_episode_id
    finally:
        await engine.stop()
        await repo.close()


@pytest.mark.asyncio
async def test_filtered_recovery_retries_only_pending_attachment(tmp_path, monkeypatch):
    config = _config(tmp_path)
    repo = await _ready_repository(config)
    service = CaptureProfileService(repo)
    filtered_profile = await service.create_profile(
        "Motion filtered",
        ["camera"],
        event_filter=["motion"],
    )
    await service.activate_profile(filtered_profile.id, source="test")
    engine = EpisodeEngine(repo, EventBus(), timeout=30, capture_profiles=service)
    await engine.start()
    try:
        no_open_time = datetime.now(timezone.utc) - timedelta(seconds=5)
        no_open_receipt = IngestionReceipt(source="filtered", received_at=no_open_time)
        await repo.create_ingestion_receipt(no_open_receipt)
        no_open_candidate = _event(timestamp=no_open_time)
        no_open = await engine.ingest_event(no_open_candidate, receipt=no_open_receipt)
        assert no_open.event.episode_id is None
        assert no_open.event.participation.attachment == ATTACHMENT_NO_OPEN_EPISODE

        opening = await engine.ingest_event(
            Event(
                device_id="camera",
                area_id="front",
                timestamp=datetime.now(timezone.utc),
                event_type="human_detection",
                source="test",
            )
        )
        episode_before = await repo.get_episode(opening.event.episode_id)
        assert episode_before is not None

        # A no-open decision is terminal even if a later mutable Episode exists.
        later_duplicate_receipt = IngestionReceipt(
            source="filtered-duplicate",
            received_at=datetime.now(timezone.utc),
        )
        await repo.create_ingestion_receipt(later_duplicate_receipt)
        no_open_duplicate = _event(timestamp=no_open_candidate.timestamp)
        no_open_duplicate.dedup_key = no_open_candidate.dedup_key
        no_open_again = await engine.ingest_event(
            no_open_duplicate,
            receipt=later_duplicate_receipt,
        )
        assert no_open_again.event.episode_id is None
        assert no_open_again.event.participation.attachment == ATTACHMENT_NO_OPEN_EPISODE

        # Simulate the second crash window: the canonical filtered Event and
        # Receipt are committed, but attribution has not run yet.
        original_attach = engine._attach_without_capture
        crashed = False

        async def crash_before_attachment(*args, **kwargs):
            nonlocal crashed
            if not crashed:
                crashed = True
                raise RuntimeError("simulated crash before filtered attachment")
            await original_attach(*args, **kwargs)

        monkeypatch.setattr(engine, "_attach_without_capture", crash_before_attachment)
        pending_time = datetime.now(timezone.utc)
        pending_receipt = IngestionReceipt(source="filtered", received_at=pending_time)
        await repo.create_ingestion_receipt(pending_receipt)
        pending_candidate = _event(timestamp=pending_time)
        with pytest.raises(RuntimeError, match="before filtered attachment"):
            await engine.ingest_event(pending_candidate, receipt=pending_receipt)

        monkeypatch.setattr(engine, "_attach_without_capture", original_attach)
        retry_receipt = IngestionReceipt(
            source="filtered-retry",
            received_at=datetime.now(timezone.utc),
        )
        await repo.create_ingestion_receipt(retry_receipt)
        retry = _event(timestamp=pending_candidate.timestamp)
        retry.dedup_key = pending_candidate.dedup_key
        attached = await engine.ingest_event(retry, receipt=retry_receipt)

        assert attached.event.episode_id == episode_before.id
        assert attached.event.participation.attachment == ATTACHMENT_ATTACHED
        episode_after = await repo.get_episode(episode_before.id)
        assert episode_after is not None
        assert episode_after.minimum_end_at == episode_before.minimum_end_at

        # Capture-excluded Events are never promoted by duplicate recovery,
        # even if the active profile later changes to include their Device.
        await _disarm(repo, service)
        excluded_receipt = IngestionReceipt(
            source="excluded",
            received_at=datetime.now(timezone.utc),
        )
        await repo.create_ingestion_receipt(excluded_receipt)
        excluded_candidate = Event(
            device_id="camera",
            area_id="front",
            timestamp=datetime.now(timezone.utc),
            event_type="vehicle_detection",
            source="test",
        )
        excluded = await engine.ingest_event(excluded_candidate, receipt=excluded_receipt)
        assert excluded.event.participation.allowed is False
        assert excluded.event.episode_id is None

        await service.activate_profile(filtered_profile.id, source="test")
        excluded_retry_receipt = IngestionReceipt(
            source="excluded-retry",
            received_at=datetime.now(timezone.utc),
        )
        await repo.create_ingestion_receipt(excluded_retry_receipt)
        excluded_retry = Event(
            device_id="camera",
            area_id="front",
            timestamp=excluded_candidate.timestamp,
            event_type="vehicle_detection",
            source="test",
            dedup_key=excluded.event.dedup_key,
        )
        excluded_again = await engine.ingest_event(
            excluded_retry,
            receipt=excluded_retry_receipt,
        )
        assert excluded_again.event.episode_id is None
        assert excluded_again.event.participation.allowed is False
    finally:
        await engine.stop()
        await repo.close()


@pytest.mark.asyncio
async def test_startup_pages_only_genuinely_pending_active_events(tmp_path, monkeypatch):
    config = _config(tmp_path)
    repo = await _ready_repository(config)
    now = datetime.now(timezone.utc)

    def decision(
        *,
        allowed: bool,
        reason: str,
        evaluated_at: datetime,
        attachment: str | None = None,
    ):
        return ParticipationDecision(
            allowed=allowed,
            profile_id="stored-profile",
            profile_name="Stored profile",
            reason=reason,
            evaluated_at=evaluated_at,
            attachment=attachment,
            activity_window_seconds=300,
        )

    for index in range(205):
        await repo.create_event(
            Event(
                device_id="camera",
                area_id="front",
                timestamp=now + timedelta(seconds=index),
                event_type=f"pending_{index}",
                participation=decision(
                    allowed=True,
                    reason="all_devices_profile",
                    evaluated_at=now + timedelta(seconds=204 - index),
                ),
                eligible_recording_device_ids=[],
            )
        )
    for index in range(250):
        await repo.create_event(
            Event(
                device_id="camera",
                area_id="front",
                timestamp=now + timedelta(minutes=10, seconds=index),
                event_type=f"excluded_{index}",
                participation=decision(
                    allowed=False,
                    reason="device_not_in_profile",
                    evaluated_at=now,
                ),
                eligible_recording_device_ids=[],
            )
        )
    for index in range(250):
        await repo.create_event(
            Event(
                device_id="camera",
                area_id="front",
                timestamp=now + timedelta(minutes=20, seconds=index),
                event_type=f"filtered_{index}",
                participation=decision(
                    allowed=False,
                    reason=FILTERED_REASON,
                    evaluated_at=now,
                    attachment=ATTACHMENT_NO_OPEN_EPISODE,
                ),
                eligible_recording_device_ids=[],
            )
        )

    engine = EpisodeEngine(repo, EventBus(), timeout=30)
    recovered_rows: list[tuple[datetime, str]] = []

    async def observe_page(candidate, *, ingress_time=None):
        recovered_rows.append((ingress_time, candidate.id))
        return candidate, False, False

    monkeypatch.setattr(engine, "_recover_unassigned_active_event", observe_page)
    try:
        await engine.start()
        assert len(recovered_rows) == 205
        assert len({event_id for _, event_id in recovered_rows}) == 205
        assert recovered_rows == sorted(recovered_rows)
        assert await repo.list_episodes() == []
        page = await repo.list_unassigned_active_events(limit=200)
        assert len(page) == 200
        assert all(event.event_type.startswith("pending_") for event in page)
        next_page = await repo.list_unassigned_active_events(
            limit=200,
            after=(page[-1].participation.evaluated_at, page[-1].id),
        )
        assert len(next_page) == 5
        assert all(event.event_type.startswith("pending_") for event in next_page)
        assert (
            await repo.list_unassigned_active_events(
                limit=200,
                after=(next_page[-1].participation.evaluated_at, next_page[-1].id),
            )
            == []
        )
    finally:
        await engine.stop()
        await repo.close()


@pytest.mark.asyncio
async def test_ambiguous_receipts_stay_unlinked_but_exact_artifact_moves_safely(tmp_path):
    config = _config(tmp_path)
    repo = await _ready_repository(config)
    received_at = datetime.now(timezone.utc)
    path = save_bytes(config.data_dir, "orphans/ingress/test", b"shared delivery", prefix="shared")
    artifact = describe_artifact(path, "event_payload", "application/octet-stream")
    first = IngestionReceipt(source="first", received_at=received_at)
    artifact, first = await repo.persist_delivery(artifact, first)
    second = IngestionReceipt(
        source="second",
        received_at=received_at + timedelta(milliseconds=1),
    )
    _, second = await repo.persist_delivery(artifact, second)

    event = _event(timestamp=received_at, raw_payload_path=artifact.file_path)
    event.participation = ParticipationDecision(
        allowed=True,
        profile_id="stored-profile",
        profile_name="Stored profile",
        reason="all_devices_profile",
        evaluated_at=received_at,
        activity_window_seconds=45,
    )
    event.eligible_recording_device_ids = ["camera"]
    event = await repo.create_event(event)

    engine = EpisodeEngine(repo, EventBus(), timeout=30)
    try:
        await engine.start()
        recovered = await repo.get_event(event.id)
        assert recovered is not None and recovered.episode_id is not None
        for receipt_id in (first.id, second.id):
            receipt = await repo.get_ingestion_receipt(receipt_id)
            assert receipt is not None
            assert receipt.event_id is None
            assert receipt.episode_id is None

        moved_artifact = await repo.get_raw_artifact(artifact.id)
        assert moved_artifact is not None
        assert moved_artifact.file_path == recovered.raw_payload_path
        assert moved_artifact.file_path.startswith(
            str(tmp_path / "episodes" / recovered.episode_id)
        )
        with open(moved_artifact.file_path, "rb") as raw_file:
            assert raw_file.read() == b"shared delivery"
    finally:
        await engine.stop()
        await repo.close()
