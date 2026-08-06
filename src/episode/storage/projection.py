from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from episode.domain.models import RawArtifact
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


class EpisodeBundleProjector:
    """Build portable, database-independent views of an Episode."""

    def __init__(self, repository: Repository, data_dir: str):
        self._repository = repository
        self._data_dir = data_dir
        self._locks: dict[str, asyncio.Lock] = {}

    async def rebuild(self) -> None:
        episodes = await self._repository.list_episodes(limit=10000)
        for episode in reversed(episodes):
            await self.refresh(episode.id)

    async def refresh(self, episode_id: str) -> None:
        lock = self._locks.setdefault(episode_id, asyncio.Lock())
        async with lock:
            manifest = await self._manifest(episode_id)
            if manifest is not None:
                await asyncio.to_thread(
                    write_manifest,
                    self._data_dir,
                    episode_id,
                    manifest,
                )

    async def _manifest(self, episode_id: str) -> dict[str, object] | None:
        episode = await self._repository.get_episode(episode_id)
        if not episode:
            return None

        events = await self._repository.list_events(episode_id=episode_id, limit=10000)
        evidence = await self._repository.list_evidence(episode_id=episode_id, limit=10000)
        receipts = await self._repository.list_ingestion_receipts(
            episode_id=episode_id,
            limit=10000,
        )
        events.sort(key=lambda item: item.timestamp)
        evidence.sort(key=lambda item: item.timestamp)

        area_ids = {episode.primary_area_id}
        area_ids.update(event.area_id for event in events if event.area_id)
        area_ids.update(item.area_id for item in evidence if item.area_id)
        device_ids = {event.device_id for event in events if event.device_id}
        device_ids.update(item.device_id for item in evidence if item.device_id)

        areas = []
        for area_id in sorted(area_ids):
            area = await self._repository.get_area(area_id)
            if area:
                areas.append({"id": area.id, "name": area.name, "location": area.location})

        devices = []
        for device_id in sorted(device_ids):
            device = await self._repository.get_device(device_id)
            if device:
                devices.append(
                    {
                        "id": device.id,
                        "name": device.name,
                        "device_type": device.device_type,
                        "area_id": device.area_id,
                        "ip_address": device.ip_address,
                    }
                )

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
                "state": episode.state.value,
                "primary_area_id": episode.primary_area_id,
                "start_time": _utc_iso(episode.start_time),
                "last_event_time": _utc_iso(episode.last_event_time),
                "last_activity_at": _utc_iso(episode.last_activity_at),
                "end_time": _utc_iso(episode.end_time),
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
                    "file": relative_bundle_path(
                        self._data_dir,
                        episode_id,
                        item.file_path,
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
