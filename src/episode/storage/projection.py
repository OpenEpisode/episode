from __future__ import annotations

import asyncio
from collections.abc import Collection
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from episode.domain.models import EpisodeState, RawArtifact
from episode.storage.bundles import relative_bundle_path, write_manifest

if TYPE_CHECKING:
    from episode.storage.repository import Repository


def _utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = (
        value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    )
    return normalized.isoformat(timespec="microseconds")


def _participation_data(event) -> dict[str, object] | None:
    decision = event.participation
    if decision is None:
        return None
    return {
        "allowed": decision.allowed,
        "profile_id": decision.profile_id,
        "profile_name": decision.profile_name,
        "reason": decision.reason,
        "evaluated_at": _utc_iso(decision.evaluated_at),
    }


class EpisodeBundleProjector:
    """Build portable, database-independent views of an Episode."""

    def __init__(self, repository: Repository, data_dir: str):
        self._repository = repository
        self._data_dir = data_dir
        self._locks: dict[str, asyncio.Lock] = {}

    async def rebuild(self, episode_ids: Collection[str] | None = None) -> None:
        """Refresh only explicitly supplied or currently unsealed Episodes.

        Closed bundles are historical records. Startup callers pass the
        unsealed IDs discovered from SQLite; the default is intentionally
        limited to the same state set for maintenance callers.
        """
        if episode_ids is None:
            episodes = []
            for state in (
                EpisodeState.ACTIVE,
                EpisodeState.QUIESCENT,
                EpisodeState.FINALIZING,
            ):
                episodes.extend(await self._repository.list_episodes(state=state, limit=10000))
            episode_ids = {episode.id for episode in episodes}
        for episode_id in sorted(set(episode_ids)):
            await self.refresh(episode_id)

    async def refresh(
        self,
        episode_id: str,
        *,
        state_override: EpisodeState | None = None,
        end_time_override: datetime | None = None,
    ) -> None:
        lock = self._locks.setdefault(episode_id, asyncio.Lock())
        async with lock:
            manifest = await self._manifest(
                episode_id,
                state_override=state_override,
                end_time_override=end_time_override,
            )
            if manifest is not None:
                await asyncio.to_thread(
                    write_manifest,
                    self._data_dir,
                    episode_id,
                    manifest,
                )

    async def _manifest(
        self,
        episode_id: str,
        *,
        state_override: EpisodeState | None = None,
        end_time_override: datetime | None = None,
    ) -> dict[str, object] | None:
        episode = await self._repository.get_episode(episode_id)
        if not episode:
            return None

        area_snapshots = await self._repository.list_episode_area_snapshots(episode_id)
        device_snapshots = await self._repository.list_episode_device_snapshots(episode_id)
        # Do not rewrite a pre-snapshot historical bundle with an incomplete
        # inventory projection. Such bundles are intentionally left as-is;
        # new Episodes always capture identity at association time.
        if (
            episode.state in {EpisodeState.CLOSED, EpisodeState.ARCHIVED}
            and not area_snapshots
            and not device_snapshots
        ):
            return None
        areas = [
            {
                "id": item["id"],
                "name": item["name"],
                "location": item["location"],
                "recorded_at": item["recorded_at"],
            }
            for item in area_snapshots
        ]
        devices = [
            {
                "id": item["id"],
                "name": item["name"],
                "device_type": item["device_type"],
                "area_id": item["area_id"],
                "ip_address": item["ip_address"],
                "recorded_at": item["recorded_at"],
            }
            for item in device_snapshots
        ]

        events = await self._repository.list_events(episode_id=episode_id, limit=10000)
        evidence = await self._repository.list_evidence(episode_id=episode_id, limit=10000)
        receipts = await self._repository.list_ingestion_receipts(
            episode_id=episode_id,
            limit=10000,
        )
        events.sort(key=lambda item: item.timestamp)
        evidence.sort(key=lambda item: item.timestamp)

        artifacts_by_id: dict[str, RawArtifact] = {}
        artifact_ids = {receipt.artifact_id for receipt in receipts if receipt.artifact_id}
        artifact_ids.update(item.artifact_id for item in evidence if item.artifact_id)
        for artifact_id in sorted(artifact_ids):
            artifact = await self._repository.get_raw_artifact(artifact_id)
            if artifact:
                artifacts_by_id[artifact.id] = artifact

        receipt_ids_by_event: dict[str, list[str]] = {}
        receipt_ids_by_evidence: dict[str, list[str]] = {}
        for receipt in receipts:
            if receipt.event_id:
                receipt_ids_by_event.setdefault(receipt.event_id, []).append(receipt.id)
            if receipt.evidence_id:
                receipt_ids_by_evidence.setdefault(receipt.evidence_id, []).append(receipt.id)

        return {
            "format": "episode.bundle",
            "episode": {
                "id": episode.id,
                "state": (state_override or episode.state).value,
                "primary_area_id": episode.primary_area_id,
                "start_time": _utc_iso(episode.start_time),
                "last_event_time": _utc_iso(episode.last_event_time),
                "last_activity_at": _utc_iso(episode.last_activity_at),
                "minimum_end_at": _utc_iso(episode.minimum_end_at),
                "end_time": _utc_iso(end_time_override or episode.end_time),
                "summary": episode.summary,
            },
            "areas": areas,
            "devices": devices,
            "events": [
                {
                    "id": event.id,
                    "timestamp": _utc_iso(event.timestamp),
                    "type": event.event_type,
                    "state": event.event_state.value,
                    "device_id": event.device_id,
                    "area_id": event.area_id,
                    "dedup_key": event.dedup_key,
                    "receipt_ids": receipt_ids_by_event.get(event.id, []),
                    "metadata": event.metadata,
                    "participation": _participation_data(event),
                    "eligible_recording_device_ids": event.eligible_recording_device_ids,
                }
                for event in events
            ],
            "evidence": [
                {
                    "id": item.id,
                    "timestamp": _utc_iso(item.timestamp),
                    "type": item.evidence_type,
                    "device_id": item.device_id,
                    "area_id": item.area_id,
                    "event_id": item.event_id,
                    "artifact_id": item.artifact_id,
                    "availability": item.availability,
                    "expired_at": _utc_iso(item.expired_at),
                    "expiration_reason": item.expiration_reason,
                    "file": (
                        relative_bundle_path(
                            self._data_dir,
                            episode_id,
                            item.file_path,
                        )
                        if item.availability == "available"
                        else None
                    ),
                    "mime_type": item.mime_type,
                    "original_filename": item.original_filename,
                    "byte_size": item.byte_size,
                    "sha256": item.sha256,
                    "receipt_ids": receipt_ids_by_evidence.get(item.id, []),
                    "metadata": item.metadata,
                }
                for item in evidence
            ],
            "receipts": [
                {
                    "id": receipt.id,
                    "source": receipt.source,
                    "received_at": _utc_iso(receipt.received_at),
                    "observed_at": _utc_iso(receipt.observed_at),
                    "status": receipt.status.value,
                    "device_id": receipt.device_id,
                    "area_id": receipt.area_id,
                    "artifact_id": receipt.artifact_id,
                    "event_id": receipt.event_id,
                    "evidence_id": receipt.evidence_id,
                    "external_id": receipt.external_id,
                    "metadata": receipt.metadata,
                }
                for receipt in receipts
            ],
            "artifacts": [
                {
                    "id": artifact.id,
                    "type": artifact.artifact_type,
                    "file": relative_bundle_path(
                        self._data_dir,
                        episode_id,
                        artifact.file_path,
                    ),
                    "mime_type": artifact.mime_type,
                    "original_filename": artifact.original_filename,
                    "byte_size": artifact.byte_size,
                    "sha256": artifact.sha256,
                    "sealed": artifact.sealed,
                    "metadata": artifact.metadata,
                }
                for artifact in artifacts_by_id.values()
            ],
        }
