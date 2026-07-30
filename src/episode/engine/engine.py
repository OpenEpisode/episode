from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING

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


class EpisodeEngine:
    def __init__(self, repo: Repository, bus: EventBus, timeout: int = 30):
        self._repo = repo
        self._bus = bus
        self._timeout = timeout
        self._running = False
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._lifecycle_lock = asyncio.Lock()

    async def start(self):
        self._running = True
        self._bus.subscribe("receipt.received", self._on_receipt_received)
        self._bus.subscribe("event.received", self._on_event_received)
        self._bus.subscribe("evidence.received", self._on_evidence_received)
        asyncio.create_task(self._timeout_loop())

    async def stop(self):
        self._running = False

    async def _persist_delivery(self, msg: Message) -> IngestionReceipt | None:
        artifact_data = msg.data.get("artifact")
        receipt_data = msg.data.get("receipt")
        if not artifact_data or not receipt_data:
            return None
        artifact = RawArtifact(**artifact_data)
        receipt = IngestionReceipt(**receipt_data)
        _, receipt = await self._repo.persist_delivery(artifact, receipt)
        return receipt

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
        receipt = await self._persist_delivery(msg)
        candidate = Event(**msg.data["event"])
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
            event, created = await self._repo.canonicalize_event(candidate)
            logger.debug(
                "Canonicalize: event=%s, created=%s, episode_id=%s",
                event.id,
                created,
                event.episode_id,
            )
            if receipt:
                await self._repo.link_ingestion_receipt(
                    receipt.id,
                    event_id=event.id,
                    episode_id=event.episode_id if not created else None,
                )
            if created:
                logger.info("Persisted canonical event %s (%s)", event.id, event.event_type)
                async with self._lifecycle_lock:
                    await self._correlate(event)
            else:
                logger.info(
                    "Linked duplicate %s delivery to canonical event %s",
                    receipt.source if receipt else candidate.source,
                    event.id,
                )

        # After lock: refresh the portable bundle and match any earlier evidence.
        if created and event.episode_id:
            await self._repo.refresh_episode_manifest(event.episode_id)

            orphan = await self._repo.find_orphan_evidence_by_device(event.device_id)
            for ev in orphan:
                await self._match_orphan_evidence(ev)

        msg.data["event"] = {
            **msg.data["event"],
            "id": event.id,
            "episode_id": event.episode_id,
        }
        msg.data["canonical_event_created"] = created

    async def _on_evidence_received(self, msg: Message):
        receipt = await self._persist_delivery(msg)
        evidence = Evidence(**msg.data["evidence"])
        await self._repo.create_evidence(evidence)
        if receipt:
            await self._repo.link_ingestion_receipt(
                receipt.id,
                evidence_id=evidence.id,
            )
        logger.info(
            "Persisted evidence %s (no event yet, episode_id=%s)", evidence.id, evidence.episode_id
        )
        if evidence.episode_id:
            logger.debug(
                "Evidence %s has pre-set episode_id=%s, linking directly",
                evidence.id,
                evidence.episode_id,
            )
            await self._repo.add_evidence_to_episode(evidence.id, evidence.episode_id)
        else:
            logger.debug("Evidence %s has no episode_id, attempting orphan match", evidence.id)
            await self._match_orphan_evidence(evidence)

    async def _correlate(self, event: Event):
        if not event.area_id:
            logger.warning("Stored event %s without an Episode: no Area", event.id)
            return
        activity_time = datetime.now(tz=timezone.utc)
        logger.debug(
            "Correlating event %s (area=%s, state=%s, timeout=%s, now=%s, event_ts=%s)",
            event.id,
            event.area_id,
            event.event_state,
            self._timeout,
            activity_time.isoformat(),
            event.timestamp,
        )
        episode = await self._repo.find_open_episode_for_area(event.area_id, self._timeout)
        logger.debug(
            "find_open_episode_for_area(%s, %s) -> %s",
            event.area_id,
            self._timeout,
            episode.id if episode else None,
        )

        if event.event_state == EventState.INACTIVE:
            if not episode:
                logger.debug(
                    "Stored inactive event %s without opening an episode",
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
            await self._bus.publish(
                Message(type="episode.updated", data={"episode_id": episode.id})
            )
            return

        if episode:
            await self._repo.add_event_to_episode(
                event.id,
                episode.id,
                _defer_manifest=True,
            )
            await self._repo.update_episode_times(
                episode.id,
                event.timestamp,
                activity_time=datetime.now(tz=timezone.utc),
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
        else:
            episode = Episode(
                id=make_episode_id(event.timestamp),
                primary_area_id=event.area_id,
                start_time=event.timestamp,
                last_event_time=event.timestamp,
                last_activity_at=activity_time,
                state=EpisodeState.ACTIVE,
            )
            await self._repo.create_episode(episode)
            await self._repo.add_event_to_episode(event.id, episode.id, _defer_manifest=True)
            await self._repo.update_episode_times(
                episode.id,
                event.timestamp,
                activity_time=datetime.now(tz=timezone.utc),
                _defer_manifest=True,
            )
            event.episode_id = episode.id
            logger.info("Created episode %s for area %s", episode.id, event.area_id)

        await self._bus.publish(Message(type="episode.updated", data={"episode_id": episode.id}))

    async def _match_orphan_evidence(self, evidence: Evidence):
        if evidence.episode_id:
            return
        if evidence.area_id:
            episode = await self._repo.find_open_episode_for_area(evidence.area_id, self._timeout)
            if episode:
                evidence.episode_id = episode.id
                await self._repo.add_evidence_to_episode(evidence.id, episode.id)
                logger.debug("Linked evidence %s to episode %s", evidence.id, episode.id)
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

    async def _timeout_loop(self):
        while self._running:
            await asyncio.sleep(1)
            try:
                async with self._lifecycle_lock:
                    closed = await self._repo.close_timed_out_episodes(self._timeout)
                for ep in closed:
                    logger.info("Episode %s closed (inactivity timeout)", ep.id)
                    await self._bus.publish(
                        Message(
                            type="episode.updated",
                            data={"episode_id": ep.id, "state": EpisodeState.CLOSED.value},
                        )
                    )
            except Exception:
                logger.exception("Error in timeout loop")

    def status(self) -> dict:
        return {"running": self._running, "timeout": self._timeout}
