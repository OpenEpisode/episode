from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from episode.capture_profiles import CaptureProfileService
from episode.domain.event_filter import (
    ATTACHMENT_ATTACHED,
    ATTACHMENT_NO_OPEN_EPISODE,
    FILTERED_REASON,
)
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
    make_episode_id,
)
from episode.engine.bus import EventBus, Message

if TYPE_CHECKING:
    from episode.storage.repository import Repository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CanonicalEventResult:
    event: Event
    created: bool
    conflict: bool = False


class EpisodeEngine:
    def __init__(
        self,
        repo: Repository,
        bus: EventBus,
        timeout: int = 30,
        quiescent_grace_seconds: int = DEFAULT_QUIESCENT_GRACE_SECONDS,
        capture_profiles: CaptureProfileService | None = None,
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

    async def start(self):
        if self._running:
            return
        self._quiescent_grace_seconds = await self._load_quiescent_grace()
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
        await self.ingest_event(candidate, receipt=receipt)

    async def ingest_event(
        self, candidate: Event, *, receipt: IngestionReceipt | None = None
    ) -> CanonicalEventResult:
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
                        if decision is not None and decision.reason == FILTERED_REASON:
                            # A class-filtered observation is still part of what
                            # happened. It must not drive capture, but it belongs
                            # to the Episode already open for this Area.
                            async with self._lifecycle_lock:
                                await self._attach_without_capture(
                                    event, activity_time=activity_time
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
        # An attached filtered Event carries its Episode id, so its journal entry
        # and manifest are written here rather than inside the area lock.
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
        await self.ingest_evidence(evidence, receipt=receipt)

    async def ingest_evidence(
        self, evidence: Evidence, *, receipt: IngestionReceipt | None = None
    ) -> Evidence:
        target_episode_id = evidence.episode_id
        # Linking owns the Episode counter, portable file move, journal, and
        # manifest update. Store preset recording Evidence as unlinked first so
        # it follows the same idempotent path as correlated snapshots.
        if target_episode_id:
            evidence.episode_id = None
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
            await self._repo.add_evidence_to_episode(evidence.id, target_episode_id)
            evidence.episode_id = target_episode_id
        else:
            logger.debug("Evidence %s has no episode_id, attempting orphan match", evidence.id)
            await self._match_orphan_evidence(evidence)
        return evidence

    async def _attach_without_capture(
        self,
        event: Event,
        *,
        activity_time: datetime,
    ) -> None:
        """Attribute a class-filtered Event to an already-open Episode.

        A filtered observation must not open an Episode, extend a deadline,
        restart a quiescent Episode, or start actions and recording — those are
        exactly what the filter exists to prevent. It should still be attributed,
        because otherwise genuinely related activity (a person walking past while
        motion noise is suppressed) shows up as unassigned noise in the timeline.

        So this path links the Event only. ``add_event_to_episode`` is idempotent
        via its ``episode_id IS NULL`` guard, and neither ``update_episode_times``
        nor ``extend_episode_minimum_end`` is called, so the Episode's lifetime is
        decided solely by Events that were allowed to drive capture.
        """
        if not event.area_id:
            return
        episode = await self._repo.find_open_episode_for_area(
            event.area_id,
            self._timeout,
            at=activity_time,
            quiescent_grace_seconds=self._quiescent_grace_seconds,
        )
        if episode is None:
            await self._record_attachment(event, ATTACHMENT_NO_OPEN_EPISODE)
            logger.debug(
                "Filtered event %s left unassigned: no open episode in area %s",
                event.id,
                event.area_id,
            )
            return
        await self._repo.add_event_to_episode(event.id, episode.id, _defer_manifest=True)
        event.episode_id = episode.id
        await self._record_attachment(event, ATTACHMENT_ATTACHED)
        logger.info(
            "Attached filtered event %s to open episode %s without extending it",
            event.id,
            episode.id,
        )
        # The caller refreshes the portable bundle once the area lock is
        # released, because it now sees this Episode id on the Event. Recording
        # and snapshot subscribers re-check ``participation.allowed``, so the
        # resulting notifications start no capture.
        await self._bus.publish(Message(type="episode.updated", data={"episode_id": episode.id}))

    async def _record_attachment(self, event: Event, attachment: str) -> None:
        """Store the attachment outcome alongside the participation decision.

        The decision is immutable, so the updated snapshot replaces it on the
        Event and is written back to the same row.
        """
        if event.participation is None:
            return
        event.participation = replace(event.participation, attachment=attachment)
        await self._repo.update_event_participation(event)

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
            if episode is None or episode.state == EpisodeState.ARCHIVED:
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
            quiescent, closed = await self._repo.transition_timed_out_episodes(
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
        for episode in closed:
            logger.info("Episode %s closed (activity policy satisfied)", episode.id)
            await self._bus.publish(
                Message(
                    type="episode.updated",
                    data={"episode_id": episode.id, "state": EpisodeState.CLOSED.value},
                )
            )

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
