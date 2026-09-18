from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from episode.capture_profiles import CaptureProfileService
from episode.domain.lifecycle import (
    DEFAULT_QUIESCENT_GRACE_SECONDS,
    MAX_QUIESCENT_GRACE_SECONDS,
    MIN_QUIESCENT_GRACE_SECONDS,
    QUIESCENT_GRACE_SETTING,
    validate_quiescent_grace_seconds,
)
from episode.domain.models import (
    Episode,
    EpisodeState,
    Event,
    EventState,
    Evidence,
    IngestionReceipt,
    RawArtifact,
    ReceiptStatus,
    make_episode_id,
)
from episode.engine.bus import EventBus, Message

if TYPE_CHECKING:
    from episode.storage.repository import Repository

logger = logging.getLogger(__name__)

EpisodeFinalizer = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class CanonicalEventResult:
    event: Event
    created: bool
    conflict: bool = False


class DeviceNeedsSetupError(ValueError):
    """A Device draft cannot contribute a new canonical observation."""


class EpisodeEngine:
    def __init__(
        self,
        repo: Repository,
        bus: EventBus,
        timeout: int = 30,
        quiescent_grace_seconds: int = DEFAULT_QUIESCENT_GRACE_SECONDS,
        capture_profiles: CaptureProfileService | None = None,
        finalizer: EpisodeFinalizer | None = None,
    ):
        self._repo = repo
        self._bus = bus
        self._timeout = timeout
        self._quiescent_grace_seconds = validate_quiescent_grace_seconds(quiescent_grace_seconds)
        self._capture_profiles = capture_profiles or CaptureProfileService(repo)
        self._running = False
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._lifecycle_lock = asyncio.Lock()
        self._timeout_task: asyncio.Task | None = None
        self._finalizer = finalizer
        self._defer_finalization = False
        self._finalizing_ids: set[str] = set()

    def set_finalizer(self, finalizer: EpisodeFinalizer | None) -> None:
        self._finalizer = finalizer

    async def start(self, *, defer_finalization: bool = False):
        if self._running:
            return
        self._quiescent_grace_seconds = await self._load_quiescent_grace()
        self._defer_finalization = defer_finalization
        self._running = True
        self._bus.subscribe("receipt.received", self._on_receipt_received)
        self._bus.subscribe("event.received", self._on_event_received)
        self._bus.subscribe("evidence.received", self._on_evidence_received)
        await self._close_timed_out_episodes()
        self._timeout_task = asyncio.create_task(
            self._timeout_loop(),
            name="episode-timeout-loop",
        )

    async def stop(self):
        if not self._running:
            return
        self._running = False
        self._bus.unsubscribe("receipt.received", self._on_receipt_received)
        self._bus.unsubscribe("event.received", self._on_event_received)
        self._bus.unsubscribe("evidence.received", self._on_evidence_received)
        if self._timeout_task:
            self._timeout_task.cancel()
            await asyncio.gather(self._timeout_task, return_exceptions=True)
            self._timeout_task = None

    async def complete_finalizing_episodes(self) -> None:
        """Complete persisted finalization after dependent services recover."""
        if not self._running:
            return
        self._defer_finalization = False
        await self._close_timed_out_episodes()

    async def _persist_delivery(self, msg: Message) -> IngestionReceipt | None:
        artifact_data = msg.data.get("artifact")
        receipt_data = msg.data.get("receipt")
        if not artifact_data or not receipt_data:
            return None
        artifact = RawArtifact(**artifact_data)
        receipt = IngestionReceipt(**receipt_data)
        _, receipt = await self._repo.persist_delivery(artifact, receipt)
        return receipt

    async def _load_quiescent_grace(self) -> int:
        return await self._repo.get_quiescent_grace_seconds(
            default=self._quiescent_grace_seconds,
        )

    def lifecycle_settings(self) -> dict[str, int | str]:
        return {
            "quiescent_grace_seconds": self._quiescent_grace_seconds,
            "default_quiescent_grace_seconds": DEFAULT_QUIESCENT_GRACE_SECONDS,
            "min_quiescent_grace_seconds": MIN_QUIESCENT_GRACE_SECONDS,
            "max_quiescent_grace_seconds": MAX_QUIESCENT_GRACE_SECONDS,
            "notice": (
                "After a Device activity window expires, Episode keeps the Area "
                "Episode and its recordings active for this brief continuation "
                "window. A new active Event during the window continues the same "
                "Episode. This is separate from each Device's activity window."
            ),
        }

    async def set_quiescent_grace(self, seconds: int) -> dict[str, int | str]:
        value = validate_quiescent_grace_seconds(seconds)
        await self._repo.set_system_setting(QUIESCENT_GRACE_SETTING, str(value))
        self._quiescent_grace_seconds = value
        if self._running:
            await self._close_timed_out_episodes()
        return self.lifecycle_settings()

    async def _on_receipt_received(self, msg: Message):
        receipt = await self._persist_delivery(msg)
        if receipt:
            logger.info(
                "Persisted %s receipt %s (%s)",
                receipt.status.value,
                receipt.id,
                receipt.source,
            )

    async def _on_event_received(self, msg: Message):
        """Compatibility adapter for connectors not yet using IngestionService."""
        receipt = await self._persist_delivery(msg)
        candidate = Event(**msg.data["event"])
        if await self._reject_draft_delivery(candidate.device_id, receipt):
            return
        await self.ingest_event(candidate, receipt=receipt)

    async def _reject_draft_delivery(
        self, device_id: str, receipt: IngestionReceipt | None
    ) -> bool:
        device = await self._repo.get_device(device_id)
        if device is None or device.setup_state != "needs_setup":
            return False
        if receipt is not None:
            receipt.status = ReceiptStatus.UNMATCHED
            receipt.device_id = device.id
            receipt.area_id = device.area_id
            receipt.metadata = {**receipt.metadata, "reason": "device_needs_setup"}
            await self._repo.update_ingestion_receipt(
                receipt.id,
                status=receipt.status,
                observed_at=receipt.observed_at,
                device_id=receipt.device_id,
                area_id=receipt.area_id,
                external_id=receipt.external_id,
                metadata=receipt.metadata,
            )
        return True

    async def ingest_event(
        self, candidate: Event, *, receipt: IngestionReceipt | None = None
    ) -> CanonicalEventResult:
        if await self._reject_draft_delivery(candidate.device_id, receipt):
            raise DeviceNeedsSetupError("Device needs setup before it can create Events")
        # Capture the observation boundary before waiting for an Area lock or
        # database work.  Correlation must use when the delivery arrived, not
        # when a busy worker eventually reaches the query.
        activity_time = (
            receipt.received_at if receipt is not None else datetime.now(tz=timezone.utc)
        )
        if activity_time.tzinfo is None:
            activity_time = activity_time.replace(tzinfo=timezone.utc)
        logger.debug(
            "Event received: id=%s, area=%s, device=%s, type=%s, state=%s, ts=%s",
            candidate.id,
            candidate.area_id,
            candidate.device_id,
            candidate.event_type,
            candidate.event_state,
            candidate.timestamp,
        )

        logger.debug(
            "Waiting for lock on area %s (event %s)",
            candidate.area_id,
            candidate.id,
        )
        async with self._locks[candidate.area_id]:
            logger.debug(
                "Acquired lock for event %s (area %s)",
                candidate.id,
                candidate.area_id,
            )
            activity_window = self._timeout
            if candidate.event_state == EventState.ACTIVE:
                device = await self._repo.get_device(candidate.device_id)
                if device and device.activity_window_seconds is not None:
                    activity_window = device.activity_window_seconds

            if candidate.event_state == EventState.ACTIVE:
                event, created = await self._capture_profiles.canonicalize_event(candidate)
            else:
                event, created = await self._repo.canonicalize_event(candidate)
            conflict = bool(
                not created
                and candidate.dedup_key
                and (
                    event.device_id != candidate.device_id
                    or event.event_type != candidate.event_type
                    or event.event_state != candidate.event_state
                )
            )
            logger.debug(
                "Canonicalize: event=%s, created=%s, conflict=%s, episode_id=%s",
                event.id,
                created,
                conflict,
                event.episode_id,
            )
            if receipt and not conflict:
                await self._repo.link_ingestion_receipt(
                    receipt.id,
                    event_id=event.id,
                    episode_id=event.episode_id if not created else None,
                )
            if conflict:
                logger.warning(
                    "Rejected conflicting delivery for canonical key %s",
                    candidate.dedup_key,
                )
            elif created:
                logger.info("Persisted canonical event %s (%s)", event.id, event.event_type)
                if event.event_state == EventState.ACTIVE:
                    decision = event.participation
                    if decision is None or not decision.allowed:
                        logger.info(
                            "Event %s excluded by capture profile %s (%s)",
                            event.id,
                            decision.profile_id if decision else "unknown",
                            decision.reason if decision else "decision_missing",
                        )
                    else:
                        async with self._lifecycle_lock:
                            await self._correlate(
                                event,
                                activity_time=activity_time,
                                activity_window=activity_window,
                            )
                else:
                    async with self._lifecycle_lock:
                        await self._correlate(
                            event,
                            activity_time=activity_time,
                            activity_window=activity_window,
                        )
            else:
                logger.info(
                    "Linked duplicate %s delivery to canonical event %s",
                    receipt.source if receipt else candidate.source,
                    event.id,
                )

        # After lock: refresh the portable bundle and match any earlier evidence.
        if created and event.episode_id and not conflict:
            await self._repo.refresh_episode_manifest(event.episode_id)

            orphan = await self._repo.find_orphan_evidence_by_device(event.device_id)
            for ev in orphan:
                await self._match_orphan_evidence(ev)

        result = CanonicalEventResult(event=event, created=created, conflict=conflict)
        if not conflict:
            await self._bus.publish(Message(type="event.canonicalized", data={"result": result}))
        return result

    async def _on_evidence_received(self, msg: Message):
        """Compatibility adapter for connectors not yet using IngestionService."""
        receipt = await self._persist_delivery(msg)
        evidence = Evidence(**msg.data["evidence"])
        if await self._reject_draft_delivery(evidence.device_id, receipt):
            return
        await self.ingest_evidence(evidence, receipt=receipt)

    async def ingest_evidence(
        self,
        evidence: Evidence,
        *,
        receipt: IngestionReceipt | None = None,
    ) -> Evidence:
        if await self._reject_draft_delivery(evidence.device_id, receipt):
            raise DeviceNeedsSetupError("Device needs setup before it can create Evidence")
        async with self._lifecycle_lock:
            return await self._ingest_evidence(
                evidence,
                receipt=receipt,
                allow_finalizing=False,
            )

    async def ingest_recording_evidence(self, evidence: Evidence) -> Evidence:
        """Persist output from a recorder finalizing an existing Episode."""
        async with self._lifecycle_lock:
            return await self._ingest_evidence(
                evidence,
                receipt=None,
                allow_finalizing=True,
            )

    async def _ingest_evidence(
        self,
        evidence: Evidence,
        *,
        receipt: IngestionReceipt | None,
        allow_finalizing: bool,
    ) -> Evidence:
        target_episode_id = evidence.episode_id
        rejected_target = False
        if target_episode_id:
            target = await self._repo.get_episode(target_episode_id)
            allowed_states = {EpisodeState.ACTIVE, EpisodeState.QUIESCENT}
            if allow_finalizing:
                allowed_states.add(EpisodeState.FINALIZING)
            if target is None or target.state not in allowed_states:
                logger.info(
                    "Preserving Evidence %s without Episode %s; state is no longer mutable",
                    evidence.id,
                    target_episode_id,
                )
                target_episode_id = None
                rejected_target = True
        if evidence.episode_id:
            evidence.episode_id = None
        persisted = await self._repo.get_evidence(evidence.id)
        if persisted:
            if target_episode_id and persisted.episode_id != target_episode_id:
                if persisted.episode_id:
                    raise RuntimeError(
                        f"Evidence {evidence.id} belongs to episode {persisted.episode_id}"
                    )
                await self._repo.add_evidence_to_episode(
                    persisted.id,
                    target_episode_id,
                    allow_finalizing=allow_finalizing,
                )
                persisted = await self._repo.get_evidence(evidence.id)
            if receipt:
                await self._repo.link_ingestion_receipt(receipt.id, evidence_id=evidence.id)
            if target_episode_id and (
                persisted is None or persisted.episode_id != target_episode_id
            ):
                raise RuntimeError(f"Evidence {evidence.id} was not associated with its Episode")
            return persisted or evidence

        await self._repo.create_evidence(evidence)
        if receipt:
            await self._repo.link_ingestion_receipt(
                receipt.id,
                evidence_id=evidence.id,
            )
        logger.info(
            "Persisted evidence %s (no event yet, episode_id=%s)", evidence.id, evidence.episode_id
        )
        if target_episode_id:
            logger.debug(
                "Evidence %s has pre-set episode_id=%s, linking directly",
                evidence.id,
                target_episode_id,
            )
            await self._repo.add_evidence_to_episode(
                evidence.id,
                target_episode_id,
                allow_finalizing=allow_finalizing,
            )
            evidence.episode_id = target_episode_id
        elif not rejected_target:
            logger.debug("Evidence %s has no episode_id, attempting orphan match", evidence.id)
            await self._match_orphan_evidence_locked(evidence)
        return evidence

    async def _correlate(
        self,
        event: Event,
        *,
        activity_time: datetime,
        activity_window: int,
    ):
        if not event.area_id:
            logger.warning("Stored event %s without an Episode: no Area", event.id)
            return
        logger.debug(
            "Correlating event %s (area=%s, state=%s, timeout=%s, now=%s, event_ts=%s)",
            event.id,
            event.area_id,
            event.event_state,
            self._timeout,
            activity_time.isoformat(),
            event.timestamp,
        )
        if event.event_state == EventState.INACTIVE:
            preceding = await self._repo.find_preceding_event_transition(event)
            if (
                preceding is None
                or preceding.event_state != EventState.ACTIVE
                or not preceding.episode_id
            ):
                logger.debug(
                    "Stored inactive event %s without a matching active transition",
                    event.id,
                )
                return

            episode = await self._repo.get_episode(preceding.episode_id)
            if episode is None or episode.state not in {
                EpisodeState.ACTIVE,
                EpisodeState.QUIESCENT,
            }:
                logger.debug(
                    "Stored inactive event %s without a mutable matching episode",
                    event.id,
                )
                return
            await self._repo.update_episode_times(
                episode.id,
                event.timestamp,
                activity_time=None,
                _defer_manifest=True,
            )
            await self._repo.add_event_to_episode(
                event.id,
                episode.id,
                _defer_manifest=True,
            )
            event.episode_id = episode.id
            logger.info(
                "Attached inactive event %s to episode %s via active event %s",
                event.id,
                episode.id,
                preceding.id,
            )
            await self._bus.publish(
                Message(type="episode.updated", data={"episode_id": episode.id})
            )
            return

        minimum_end_at = activity_time + timedelta(seconds=activity_window)
        episode = await self._repo.find_open_episode_for_area(
            event.area_id,
            self._timeout,
            at=activity_time,
            quiescent_grace_seconds=self._quiescent_grace_seconds,
        )
        logger.debug(
            "find_open_episode_for_area(%s, %s) -> %s",
            event.area_id,
            self._timeout,
            episode.id if episode else None,
        )

        if episode:
            await self._repo.add_event_to_episode(
                event.id,
                episode.id,
                _defer_manifest=True,
            )
            await self._repo.update_episode_times(
                episode.id,
                event.timestamp,
                activity_time=activity_time,
                _defer_manifest=True,
            )
            await self._repo.extend_episode_minimum_end(
                episode.id,
                minimum_end_at,
                _defer_manifest=True,
            )
            if episode.state == EpisodeState.QUIESCENT:
                await self._repo.update_episode_state(
                    episode.id,
                    EpisodeState.ACTIVE,
                    _defer_manifest=True,
                )
            event.episode_id = episode.id
            logger.debug(
                "Added event %s to existing episode %s",
                event.id,
                episode.id,
            )
            episode_created = False
        else:
            episode = Episode(
                id=make_episode_id(event.timestamp),
                primary_area_id=event.area_id,
                start_time=event.timestamp,
                last_event_time=event.timestamp,
                last_activity_at=activity_time,
                minimum_end_at=minimum_end_at,
                state=EpisodeState.ACTIVE,
            )
            await self._repo.create_episode(episode)
            await self._repo.add_event_to_episode(event.id, episode.id, _defer_manifest=True)
            await self._repo.update_episode_times(
                episode.id,
                event.timestamp,
                activity_time=activity_time,
                _defer_manifest=True,
            )
            await self._repo.extend_episode_minimum_end(
                episode.id,
                minimum_end_at,
                _defer_manifest=True,
            )
            event.episode_id = episode.id
            logger.info("Created episode %s for area %s", episode.id, event.area_id)
            episode_created = True

        if episode_created:
            await self._bus.publish(
                Message(
                    type="episode.created",
                    data={"episode_id": episode.id, "event_id": event.id},
                )
            )
        await self._bus.publish(Message(type="episode.updated", data={"episode_id": episode.id}))

    async def _match_orphan_evidence(self, evidence: Evidence):
        async with self._lifecycle_lock:
            await self._match_orphan_evidence_locked(evidence)

    async def _match_orphan_evidence_locked(self, evidence: Evidence):
        if evidence.episode_id:
            return
        if evidence.area_id:
            # FTP and similar transports may deliver queued media after an
            # Episode closes. Prefer the source observation time so delayed
            # Evidence joins the Episode during which it was captured.
            episode = await self._repo.find_episode_for_area_at(
                evidence.area_id,
                evidence.timestamp,
            )
            if episode is None:
                # Preserve the established behavior for sources whose clocks
                # are slightly ahead of or behind the Event source.
                episode = await self._repo.find_open_episode_for_area(
                    evidence.area_id,
                    self._timeout,
                    quiescent_grace_seconds=self._quiescent_grace_seconds,
                )
            if episode:
                evidence.episode_id = episode.id
                await self._repo.add_evidence_to_episode(evidence.id, episode.id)
                logger.debug(
                    "Linked evidence %s to %s episode %s",
                    evidence.id,
                    episode.state.value,
                    episode.id,
                )
                await self._bus.publish(
                    Message(
                        type="episode.updated",
                        data={"episode_id": episode.id, "evidence_id": evidence.id},
                    )
                )
                return

        if evidence.episode_id:
            await self._bus.publish(
                Message(
                    type="episode.updated",
                    data={
                        "episode_id": evidence.episode_id,
                        "evidence_id": evidence.id,
                    },
                )
            )

    async def _close_timed_out_episodes(self, *, now: datetime | None = None) -> None:
        async with self._lifecycle_lock:
            quiescent, finalizing = await self._repo.transition_timed_out_episodes(
                self._timeout,
                self._quiescent_grace_seconds,
                now=now,
            )
        for episode in quiescent:
            logger.info("Episode %s entered quiescence", episode.id)
            await self._bus.publish(
                Message(
                    type="episode.updated",
                    data={"episode_id": episode.id, "state": EpisodeState.QUIESCENT.value},
                )
            )
        if self._defer_finalization:
            return
        for episode in finalizing:
            if episode.id in self._finalizing_ids:
                continue
            self._finalizing_ids.add(episode.id)
            logger.info("Episode %s entered finalization", episode.id)
            try:
                await self._bus.publish(
                    Message(
                        type="episode.updated",
                        data={
                            "episode_id": episode.id,
                            "state": EpisodeState.FINALIZING.value,
                        },
                    )
                )
                if self._finalizer:
                    await self._finalizer(episode.id)
                closed_at = datetime.now(tz=timezone.utc)
                await self._repo.refresh_episode_manifest(
                    episode.id,
                    state_override=EpisodeState.CLOSED,
                    end_time_override=closed_at,
                )
                await self._repo.mark_episode_closed(episode.id, ended_at=closed_at)
            except Exception:
                logger.exception(
                    "Episode %s finalization failed; leaving it finalizing",
                    episode.id,
                )
            else:
                logger.info("Episode %s closed (finalization succeeded)", episode.id)
                await self._bus.publish(
                    Message(
                        type="episode.updated",
                        data={"episode_id": episode.id, "state": EpisodeState.CLOSED.value},
                    )
                )
            finally:
                self._finalizing_ids.discard(episode.id)

    async def _timeout_loop(self):
        while self._running:
            await asyncio.sleep(1)
            try:
                await self._close_timed_out_episodes()
            except Exception:
                logger.exception("Error in timeout loop")

    def status(self) -> dict:
        return {
            "running": self._running,
            "timeout": self._timeout,
            "quiescent_grace_seconds": self._quiescent_grace_seconds,
        }
