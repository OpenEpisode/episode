from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
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

        async with self._locks[candidate.area_id]:
            event, created = await self._repo.canonicalize_event(candidate)
            if receipt:
                await self._repo.link_ingestion_receipt(
                    receipt.id,
                    event_id=event.id,
                    episode_id=event.episode_id if not created else None,
                )
            if created:
                logger.info("Persisted canonical event %s (%s)", event.id, event.event_type)
                await self._correlate(event)
            else:
                logger.info(
                    "Linked duplicate %s delivery to canonical event %s",
                    receipt.source if receipt else candidate.source,
                    event.id,
                )

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
        logger.info("Persisted evidence %s (no event yet)", evidence.id)
        if evidence.episode_id:
            await self._repo.add_evidence_to_episode(evidence.id, evidence.episode_id)
        else:
            await self._match_orphan_evidence(evidence)

    async def _correlate(self, event: Event):
        episode = await self._repo.find_open_episode_for_area(event.area_id, self._timeout)

        if not episode:
            episode = await self._repo.find_any_open_episode(self._timeout)

        if event.event_state == EventState.INACTIVE:
            if not episode:
                logger.debug(
                    "Stored inactive event %s without opening an episode",
                    event.id,
                )
                return
            await self._repo.add_event_to_episode(event.id, episode.id)
            event.episode_id = episode.id
            await self._bus.publish(
                Message(type="episode.updated", data={"episode_id": episode.id})
            )
            return

        if episode:
            await self._repo.add_event_to_episode(event.id, episode.id)
            await self._repo.update_episode_last_event(episode.id, event.timestamp)
            if episode.state == EpisodeState.QUIESCENT:
                await self._repo.update_episode_state(episode.id, EpisodeState.ACTIVE)
            event.episode_id = episode.id
            logger.debug(
                "Added event %s to %s episode %s",
                event.id,
                "existing" if episode.primary_area_id == event.area_id else "cross-area",
                episode.id,
            )
        else:
            episode = Episode(
                id=make_episode_id(event.timestamp),
                primary_area_id=event.area_id,
                start_time=event.timestamp,
                last_event_time=event.timestamp,
                state=EpisodeState.ACTIVE,
            )
            await self._repo.create_episode(episode)
            await self._repo.add_event_to_episode(event.id, episode.id)
            event.episode_id = episode.id
            logger.info("Created episode %s for area %s", episode.id, event.area_id)

        await self._bus.publish(Message(type="episode.updated", data={"episode_id": episode.id}))

        # Re-check orphan evidence that may have arrived before events
        orphan = await self._repo.find_orphan_evidence_by_device(event.device_id)
        for ev in orphan:
            await self._match_orphan_evidence(ev)

    async def _match_orphan_evidence(self, evidence: Evidence):
        if evidence.episode_id:
            return
        if evidence.area_id:
            episode = await self._repo.find_open_episode_for_area(evidence.area_id, self._timeout)
            if not episode:
                episode = await self._repo.find_any_open_episode(self._timeout)
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
