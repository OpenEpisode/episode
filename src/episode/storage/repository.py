from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import aiosqlite

from episode.config import EpisodeConfig
from episode.domain.models import (
    Area,
    Device,
    Episode,
    EpisodeState,
    Event,
    Evidence,
    IngestionReceipt,
    RawArtifact,
    ReceiptStatus,
)
from episode.storage.bundles import append_journal
from episode.storage.database import SCHEMA_SQL
from episode.storage.events import EventStore
from episode.storage.files import async_move_to_episode, describe_artifact
from episode.storage.inventory import InventoryStore
from episode.storage.projection import EpisodeBundleProjector
from episode.storage.provenance import ProvenanceStore
from episode.storage.recovery import reconcile_episode_counts, reconcile_episode_paths

logger = logging.getLogger(__name__)


SQLITE_BUSY_TIMEOUT_MS = 15_000


def _utc_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    normalized = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return normalized.isoformat(timespec="microseconds")


class Repository:
    def __init__(self, config: EpisodeConfig):
        self._db_path = config.db_path
        self._data_dir = config.data_dir
        self._conn: aiosqlite.Connection | None = None
        self._provenance: ProvenanceStore | None = None
        self._inventory: InventoryStore | None = None
        self._events: EventStore | None = None
        self._delivery_conn: aiosqlite.Connection | None = None
        self._delivery_provenance: ProvenanceStore | None = None
        self._delivery_lock = asyncio.Lock()
        self._bundles = EpisodeBundleProjector(self, self._data_dir)

    async def initialize(self):
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        os.makedirs(os.path.join(self._data_dir, "episodes"), exist_ok=True)
        os.makedirs(os.path.join(self._data_dir, "orphans", "snapshots"), exist_ok=True)
        os.makedirs(os.path.join(self._data_dir, "orphans", "events"), exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
        journal_mode = await self._conn.execute_fetchall("PRAGMA journal_mode = WAL")
        if not journal_mode or str(journal_mode[0][0]).lower() != "wal":
            raise RuntimeError("Episode requires SQLite WAL mode")
        await self._conn.execute("PRAGMA synchronous = NORMAL")
        await self._conn.executescript(SCHEMA_SQL)
        self._provenance = ProvenanceStore(self._conn)
        self._inventory = InventoryStore(self._conn)
        self._events = EventStore(self._conn)
        await self._conn.commit()
        await self._enable_foreign_keys(self._conn)
        # Raw artifacts and their receipts use a dedicated connection so their
        # transaction cannot be committed accidentally by another repository
        # coroutine sharing the main connection.
        self._delivery_conn = await aiosqlite.connect(self._db_path)
        self._delivery_conn.row_factory = aiosqlite.Row
        await self._delivery_conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
        await self._delivery_conn.execute("PRAGMA synchronous = NORMAL")
        self._delivery_provenance = ProvenanceStore(self._delivery_conn)

        await reconcile_episode_paths(
            self._conn,
            self._provenance,
            self._data_dir,
        )
        await reconcile_episode_counts(self._conn)
        await self.rebuild_episode_manifests()

    @staticmethod
    async def _enable_foreign_keys(connection: aiosqlite.Connection) -> None:
        await connection.execute("PRAGMA foreign_keys = ON")
        enabled = await connection.execute_fetchall("PRAGMA foreign_keys")
        if not enabled or enabled[0][0] != 1:
            raise RuntimeError("Episode requires SQLite foreign-key enforcement")

    async def close(self):
        if self._delivery_conn:
            await self._delivery_conn.close()
            self._delivery_conn = None
            self._delivery_provenance = None
        if self._conn:
            await self._conn.close()
            self._conn = None
            self._provenance = None
            self._inventory = None
            self._events = None

    # --- Provenance ---

    async def create_raw_artifact(self, artifact: RawArtifact) -> RawArtifact:
        if self._provenance is None:
            raise RuntimeError("Repository is not initialized")
        return await self._provenance.create_artifact(artifact)

    async def get_raw_artifact(self, artifact_id: str) -> RawArtifact | None:
        if self._provenance is None:
            raise RuntimeError("Repository is not initialized")
        return await self._provenance.get_artifact(artifact_id)

    async def create_ingestion_receipt(self, receipt: IngestionReceipt) -> IngestionReceipt:
        if self._provenance is None:
            raise RuntimeError("Repository is not initialized")
        return await self._provenance.create_receipt(receipt)

    async def persist_delivery(
        self, artifact: RawArtifact, receipt: IngestionReceipt
    ) -> tuple[RawArtifact, IngestionReceipt]:
        if self._delivery_provenance is None or self._delivery_conn is None:
            raise RuntimeError("Repository is not initialized")
        async with self._delivery_lock:
            try:
                await self._delivery_conn.execute("BEGIN IMMEDIATE")
                stored_artifact = await self._delivery_provenance.create_artifact(
                    artifact,
                    commit=False,
                )
                receipt.artifact_id = stored_artifact.id
                await self._delivery_provenance.create_receipt(receipt, commit=False)
                await self._delivery_conn.commit()
            except BaseException:
                # CancelledError is a BaseException. Without this rollback, a
                # cancelled connector task can retain SQLite's write lock for
                # the lifetime of the process and stall every other ingress.
                try:
                    await asyncio.shield(self._delivery_conn.rollback())
                except Exception:
                    logger.exception("Could not roll back interrupted delivery transaction")
                raise
        return stored_artifact, receipt

    async def list_ingestion_receipts(
        self,
        *,
        episode_id: str | None = None,
        event_id: str | None = None,
        evidence_id: str | None = None,
        source: str | None = None,
        status: ReceiptStatus | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[IngestionReceipt]:
        if self._provenance is None:
            raise RuntimeError("Repository is not initialized")
        return await self._provenance.list_receipts(
            episode_id=episode_id,
            event_id=event_id,
            evidence_id=evidence_id,
            source=source,
            status=status,
            limit=limit,
            offset=offset,
        )

    async def get_ingestion_receipt(self, receipt_id: str) -> IngestionReceipt | None:
        if self._provenance is None:
            raise RuntimeError("Repository is not initialized")
        return await self._provenance.get_receipt(receipt_id)

    async def update_ingestion_receipt(
        self,
        receipt_id: str,
        *,
        status: ReceiptStatus,
        observed_at: datetime | None,
        device_id: str,
        area_id: str,
        external_id: str | None,
        metadata: dict,
    ) -> None:
        if self._provenance is None:
            raise RuntimeError("Repository is not initialized")
        await self._provenance.update_receipt(
            receipt_id,
            status=status,
            observed_at=observed_at,
            device_id=device_id,
            area_id=area_id,
            external_id=external_id,
            metadata=metadata,
        )

    async def link_ingestion_receipt(
        self,
        receipt_id: str,
        *,
        event_id: str | None = None,
        evidence_id: str | None = None,
        episode_id: str | None = None,
    ) -> None:
        if self._provenance is None:
            raise RuntimeError("Repository is not initialized")
        await self._provenance.link_receipt(
            receipt_id,
            event_id=event_id,
            evidence_id=evidence_id,
            episode_id=episode_id,
        )
        if episode_id:
            await self._move_receipt_artifact(receipt_id, episode_id)
            await asyncio.to_thread(
                append_journal,
                self._data_dir,
                episode_id,
                "receipt.added",
                {"receipt_id": receipt_id, "event_id": event_id, "evidence_id": evidence_id},
            )
            await self.refresh_episode_manifest(episode_id)

    async def _move_receipt_artifact(self, receipt_id: str, episode_id: str) -> None:
        if self._provenance is None:
            raise RuntimeError("Repository is not initialized")
        receipt = await self._provenance.get_receipt(receipt_id)
        if not receipt or not receipt.artifact_id:
            return
        artifact = await self._provenance.get_artifact(receipt.artifact_id)
        if not artifact:
            return
        subdir = {
            "event_payload": "events",
            "snapshot": "snapshots",
            "recording": "recordings",
        }.get(artifact.artifact_type, "other")
        new_path = await async_move_to_episode(
            self._data_dir, episode_id, artifact.file_path, subdir
        )
        if new_path != artifact.file_path:
            await self._provenance.update_artifact_path(artifact.id, new_path)

    async def _describe_existing_artifact(
        self,
        path: str,
        artifact_type: str,
        mime_type: str,
        original_filename: str | None = None,
    ) -> RawArtifact | None:
        if not path or not os.path.isfile(path):
            return None
        if self._provenance is None:
            raise RuntimeError("Repository is not initialized")
        existing = await self._provenance.find_artifact_by_path(path)
        if existing:
            return existing
        artifact = await asyncio.to_thread(
            describe_artifact,
            path,
            artifact_type,
            mime_type,
            original_filename=original_filename,
        )
        return await self.create_raw_artifact(artifact)

    # --- Areas ---

    async def upsert_area(self, area: Area) -> Area:
        return await self._inventory_store().upsert_area(area)

    async def get_area(self, area_id: str) -> Area | None:
        return await self._inventory_store().get_area(area_id)

    async def list_areas(self, *, include_disabled: bool = False) -> list[Area]:
        return await self._inventory_store().list_areas(include_disabled=include_disabled)

    async def delete_area(self, area_id: str) -> None:
        await self._inventory_store().delete_area(area_id)

    # --- Devices ---

    async def upsert_device(self, device: Device) -> Device:
        return await self._inventory_store().upsert_device(device)

    async def get_device(self, device_id: str) -> Device | None:
        return await self._inventory_store().get_device(device_id)

    async def find_device_by_ip(self, ip_address: str) -> Device | None:
        return await self._inventory_store().find_device_by_ip(ip_address)

    async def list_devices(
        self,
        area_id: str | None = None,
        *,
        include_disabled: bool = False,
    ) -> list[Device]:
        return await self._inventory_store().list_devices(
            area_id,
            include_disabled=include_disabled,
        )

    async def delete_device(self, device_id: str) -> None:
        await self._inventory_store().delete_device(device_id)

    async def area_usage(self, area_id: str) -> dict[str, int]:
        return await self._inventory_store().area_usage(area_id)

    async def device_usage(self, device_id: str) -> dict[str, int]:
        return await self._inventory_store().device_usage(device_id)

    def _inventory_store(self) -> InventoryStore:
        if self._inventory is None:
            raise RuntimeError("Repository is not initialized")
        return self._inventory

    # --- Events ---

    async def create_event(self, event: Event) -> Event:
        return await self._event_store().create(event)

    async def canonicalize_event(self, event: Event) -> tuple[Event, bool]:
        return await self._event_store().canonicalize(event)

    async def get_event(self, event_id: str) -> Event | None:
        return await self._event_store().get(event_id)

    async def find_event_by_dedup_key(self, dedup_key: str) -> Event | None:
        return await self._event_store().find_by_dedup_key(dedup_key)

    async def list_events(
        self,
        episode_id: str | None = None,
        area_id: str | None = None,
        device_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
        *,
        event_type: str | None = None,
        event_state: str | None = None,
        has_episode: bool | None = None,
    ) -> list[Event]:
        return await self._event_store().list(
            episode_id,
            area_id,
            device_id,
            limit,
            offset,
            event_type=event_type,
            event_state=event_state,
            has_episode=has_episode,
        )

    async def find_recent_events_by_device(self, device_id: str, since: datetime) -> list[Event]:
        return await self._event_store().find_recent_by_device(device_id, since)

    async def update_event_episode(self, event_id: str, episode_id: str) -> None:
        await self._event_store().update_episode(event_id, episode_id)

    def _event_store(self) -> EventStore:
        if self._events is None:
            raise RuntimeError("Repository is not initialized")
        return self._events

    # --- Evidence ---

    async def create_evidence(self, evidence: Evidence) -> Evidence:
        if not evidence.artifact_id:
            artifact = await self._describe_existing_artifact(
                evidence.file_path,
                evidence.evidence_type or "evidence",
                evidence.mime_type or "application/octet-stream",
                evidence.original_filename,
            )
            if artifact:
                evidence.artifact_id = artifact.id
                evidence.byte_size = artifact.byte_size
                evidence.sha256 = artifact.sha256

        await self._conn.execute(
            """INSERT INTO evidence (
                id, device_id, area_id, timestamp,
                evidence_type, file_path, mime_type, original_filename,
                artifact_id, byte_size, sha256, metadata, event_id, episode_id
            )
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                evidence.id,
                evidence.device_id,
                evidence.area_id,
                _utc_iso(evidence.timestamp),
                evidence.evidence_type,
                evidence.file_path,
                evidence.mime_type,
                evidence.original_filename,
                evidence.artifact_id,
                evidence.byte_size,
                evidence.sha256,
                json.dumps(evidence.metadata),
                evidence.event_id,
                evidence.episode_id,
            ),
        )
        await self._conn.commit()
        return evidence

    async def get_evidence(self, evidence_id: str) -> Evidence | None:
        row = await self._conn.execute_fetchall(
            "SELECT * FROM evidence WHERE id = ?", (evidence_id,)
        )
        if not row:
            return None
        return self._row_to_evidence(row[0])

    async def list_evidence(
        self,
        episode_id: str | None = None,
        event_id: str | None = None,
        device_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
        *,
        area_id: str | None = None,
        evidence_type: str | None = None,
        has_episode: bool | None = None,
    ) -> list[Evidence]:
        clauses = []
        params = []
        if episode_id:
            clauses.append("episode_id = ?")
            params.append(episode_id)
        if event_id:
            clauses.append("event_id = ?")
            params.append(event_id)
        if device_id:
            clauses.append("device_id = ?")
            params.append(device_id)
        if area_id:
            clauses.append("area_id = ?")
            params.append(area_id)
        if evidence_type:
            clauses.append("evidence_type = ?")
            params.append(evidence_type)
        if has_episode is not None:
            clauses.append("episode_id IS NOT NULL" if has_episode else "episode_id IS NULL")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = await self._conn.execute_fetchall(
            f"SELECT * FROM evidence{where} ORDER BY timestamp DESC, id DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        return [self._row_to_evidence(r) for r in rows]

    async def episode_covers(self, episode_ids: list[str]) -> dict[str, str]:
        if not episode_ids:
            return {}
        placeholders = ",".join("?" for _item in episode_ids)
        rows = await self._conn.execute_fetchall(
            f"""SELECT e.episode_id, e.id AS evidence_id
                FROM evidence e
                INNER JOIN (
                  SELECT episode_id, MIN(timestamp) AS min_ts
                  FROM evidence
                  WHERE episode_id IN ({placeholders}) AND mime_type LIKE 'image/%'
                  GROUP BY episode_id
                ) first_image
                  ON e.episode_id = first_image.episode_id
                 AND e.timestamp = first_image.min_ts
                WHERE e.mime_type LIKE 'image/%'""",
            episode_ids,
        )
        return {row["episode_id"]: row["evidence_id"] for row in rows}

    async def find_orphan_evidence(self, older_than: timedelta | None = None) -> list[Evidence]:
        rows = await self._conn.execute_fetchall(
            "SELECT * FROM evidence WHERE event_id IS NULL ORDER BY timestamp ASC"
        )
        return [self._row_to_evidence(r) for r in rows]

    async def find_orphan_evidence_by_device(self, device_id: str) -> list[Evidence]:
        rows = await self._conn.execute_fetchall(
            """SELECT * FROM evidence
               WHERE device_id = ? AND event_id IS NULL AND episode_id IS NULL
               ORDER BY timestamp ASC""",
            (device_id,),
        )
        return [self._row_to_evidence(r) for r in rows]

    async def update_evidence_episode(self, evidence_id: str, episode_id: str):
        await self._conn.execute(
            "UPDATE evidence SET episode_id = ? WHERE id = ?",
            (episode_id, evidence_id),
        )
        await self._conn.commit()

    async def update_evidence_event(self, evidence_id: str, event_id: str):
        await self._conn.execute(
            "UPDATE evidence SET event_id = ? WHERE id = ?",
            (event_id, evidence_id),
        )
        await self._conn.commit()

    # --- Episodes ---

    async def create_episode(self, episode: Episode) -> Episode:
        await self._conn.execute(
            """INSERT INTO episodes (
                id, primary_area_id, start_time,
                last_event_time, last_activity_at, end_time, state,
                event_count, evidence_count, summary
            )
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                episode.id,
                episode.primary_area_id,
                _utc_iso(episode.start_time),
                _utc_iso(episode.last_event_time),
                _utc_iso(episode.last_activity_at),
                _utc_iso(episode.end_time),
                episode.state.value,
                episode.event_count,
                episode.evidence_count,
                episode.summary,
            ),
        )
        await self._conn.commit()
        await asyncio.to_thread(
            append_journal,
            self._data_dir,
            episode.id,
            "episode.created",
            {"primary_area_id": episode.primary_area_id},
        )
        await self.refresh_episode_manifest(episode.id)
        return episode

    async def get_episode(self, episode_id: str) -> Episode | None:
        row = await self._conn.execute_fetchall(
            "SELECT * FROM episodes WHERE id = ?", (episode_id,)
        )
        if not row:
            return None
        return self._row_to_episode(row[0])

    async def list_episodes(
        self,
        area_id: str | None = None,
        state: EpisodeState | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Episode]:
        clauses = []
        params = []
        if area_id:
            clauses.append("primary_area_id = ?")
            params.append(area_id)
        if state:
            clauses.append("state = ?")
            params.append(state.value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = await self._conn.execute_fetchall(
            f"SELECT * FROM episodes{where} ORDER BY start_time DESC, id DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        return [self._row_to_episode(r) for r in rows]

    async def episode_trigger_event_types(self, episode_ids: list[str]) -> dict[str, str]:
        """Return the first active Event type for each requested Episode."""
        if not episode_ids:
            return {}
        placeholders = ",".join("?" for _item in episode_ids)
        rows = await self._conn.execute_fetchall(
            f"""SELECT episode_id, event_type
                FROM (
                    SELECT episode_id, event_type,
                           ROW_NUMBER() OVER (
                               PARTITION BY episode_id
                               ORDER BY timestamp ASC, id ASC
                           ) AS event_order
                    FROM events
                    WHERE episode_id IN ({placeholders})
                      AND event_state = 'active'
                )
                WHERE event_order = 1""",
            episode_ids,
        )
        return {row["episode_id"]: row["event_type"] for row in rows}

    async def find_open_episode_for_area(self, area_id: str, timeout: int) -> Episode | None:
        cutoff = _utc_iso(datetime.now(tz=timezone.utc) - timedelta(seconds=timeout))
        rows = await self._conn.execute_fetchall(
            """SELECT * FROM episodes
               WHERE primary_area_id = ?
               AND state IN ('active', 'quiescent')
               AND julianday(COALESCE(last_activity_at, last_event_time, start_time))
                   >= julianday(?)
               ORDER BY julianday(
                   COALESCE(last_activity_at, last_event_time, start_time)
               ) DESC
               LIMIT 1""",
            (area_id, cutoff),
        )
        episode = self._row_to_episode(rows[0]) if rows else None
        logger.debug(
            "Open episode for area %s: %s (cutoff=%s, activity=%s)",
            area_id,
            episode.id if episode else None,
            cutoff,
            episode.last_activity_at if episode else None,
        )
        return episode

    async def find_episode_for_area_at(
        self,
        area_id: str,
        timestamp: datetime,
    ) -> Episode | None:
        """Find a mutable Episode whose recorded lifespan contains an observation."""
        observed_at = _utc_iso(timestamp)
        rows = await self._conn.execute_fetchall(
            """SELECT * FROM episodes
               WHERE primary_area_id = ?
                 AND state IN ('active', 'quiescent', 'closed')
                 AND julianday(start_time) <= julianday(?)
                 AND (
                     end_time IS NULL
                     OR julianday(?) <= julianday(end_time)
                 )
               ORDER BY julianday(start_time) DESC
               LIMIT 1""",
            (area_id, observed_at, observed_at),
        )
        return self._row_to_episode(rows[0]) if rows else None

    async def find_recent_closed_episode_for_inactive(
        self,
        event: Event,
        grace_seconds: int,
    ) -> Episode | None:
        """Find the Episode whose matching active transition a late inactive closes."""
        cutoff = _utc_iso(datetime.now(tz=timezone.utc) - timedelta(seconds=grace_seconds))
        event_time = _utc_iso(event.timestamp)
        rows = await self._conn.execute_fetchall(
            """SELECT episode.*
               FROM episodes AS episode
               WHERE episode.primary_area_id = ?
                 AND episode.state = 'closed'
                 AND julianday(episode.end_time) >= julianday(?)
                 AND julianday(?) >= julianday(episode.start_time)
                 AND EXISTS (
                     SELECT 1
                     FROM events AS active_event
                     WHERE active_event.episode_id = episode.id
                       AND active_event.device_id = ?
                       AND active_event.event_type = ?
                       AND active_event.event_state = 'active'
                 )
               ORDER BY julianday(episode.end_time) DESC
               LIMIT 1""",
            (
                event.area_id,
                cutoff,
                event_time,
                event.device_id,
                event.event_type,
            ),
        )
        return self._row_to_episode(rows[0]) if rows else None

    async def add_event_to_episode(
        self, event_id: str, episode_id: str, *, _defer_manifest: bool = False
    ):
        event = await self.get_event(event_id)
        if not event:
            return
        if event.episode_id == episode_id:
            return
        if event.episode_id:
            raise ValueError(f"Event {event_id} already belongs to episode {event.episode_id}")
        cursor = await self._conn.execute(
            "UPDATE events SET episode_id = ? WHERE id = ? AND episode_id IS NULL",
            (episode_id, event_id),
        )
        if cursor.rowcount != 1:
            await self._conn.rollback()
            current = await self.get_event(event_id)
            if current and current.episode_id == episode_id:
                return
            raise RuntimeError(f"Event {event_id} could not be linked to episode {episode_id}")
        await self._conn.execute(
            "UPDATE episodes SET event_count = event_count + 1 WHERE id = ?",
            (episode_id,),
        )
        await self._conn.commit()

        raw_payload_path = event.raw_payload_path
        receipts = await self.list_ingestion_receipts(event_id=event_id)
        for receipt in receipts:
            if self._provenance is not None:
                await self._provenance.link_receipt(
                    receipt.id, event_id=event_id, episode_id=episode_id
                )
            if receipt.artifact_id and self._provenance is not None:
                artifact = await self._provenance.get_artifact(receipt.artifact_id)
                if artifact:
                    old_path = artifact.file_path
                    new_path = await async_move_to_episode(
                        self._data_dir, episode_id, old_path, "events"
                    )
                    if new_path != old_path:
                        await self._provenance.update_artifact_path(artifact.id, new_path)
                    if raw_payload_path == old_path:
                        raw_payload_path = new_path

        if raw_payload_path and not receipts:
            raw_payload_path = await async_move_to_episode(
                self._data_dir, episode_id, raw_payload_path, "events"
            )
        if raw_payload_path != event.raw_payload_path:
            await self._conn.execute(
                "UPDATE events SET raw_payload_path = ? WHERE id = ?",
                (raw_payload_path, event_id),
            )
            await self._conn.commit()
        await asyncio.to_thread(
            append_journal,
            self._data_dir,
            episode_id,
            "event.added",
            {"event_id": event_id},
        )
        if not _defer_manifest:
            await self.refresh_episode_manifest(episode_id)

    async def add_evidence_to_episode(
        self, evidence_id: str, episode_id: str, *, _defer_manifest: bool = False
    ):
        evidence = await self.get_evidence(evidence_id)
        if not evidence:
            return
        if evidence.episode_id == episode_id:
            return
        if evidence.episode_id:
            raise ValueError(
                f"Evidence {evidence_id} already belongs to episode {evidence.episode_id}"
            )
        cursor = await self._conn.execute(
            "UPDATE evidence SET episode_id = ? WHERE id = ? AND episode_id IS NULL",
            (episode_id, evidence_id),
        )
        if cursor.rowcount != 1:
            await self._conn.rollback()
            current = await self.get_evidence(evidence_id)
            if current and current.episode_id == episode_id:
                return
            raise RuntimeError(
                f"Evidence {evidence_id} could not be linked to episode {episode_id}"
            )
        await self._conn.execute(
            "UPDATE episodes SET evidence_count = evidence_count + 1 WHERE id = ?",
            (episode_id,),
        )
        await self._conn.commit()

        receipts = await self.list_ingestion_receipts(evidence_id=evidence_id)
        if self._provenance is not None:
            for receipt in receipts:
                await self._provenance.link_receipt(
                    receipt.id, evidence_id=evidence_id, episode_id=episode_id
                )

        new_path = evidence.file_path
        if evidence.file_path:
            subdir = {
                "snapshot": "snapshots",
                "recording": "recordings",
            }.get(evidence.evidence_type, "other")
            new_path = await async_move_to_episode(
                self._data_dir, episode_id, evidence.file_path, subdir
            )
            if (
                evidence.artifact_id
                and self._provenance is not None
                and new_path != evidence.file_path
            ):
                await self._provenance.update_artifact_path(evidence.artifact_id, new_path)

        if new_path != evidence.file_path:
            await self._conn.execute(
                "UPDATE evidence SET file_path = ? WHERE id = ?",
                (new_path, evidence_id),
            )
            await self._conn.commit()
        await asyncio.to_thread(
            append_journal,
            self._data_dir,
            episode_id,
            "evidence.added",
            {"evidence_id": evidence_id},
        )
        if not _defer_manifest:
            await self.refresh_episode_manifest(episode_id)

    async def update_episode_state(
        self, episode_id: str, state: EpisodeState, *, _defer_manifest: bool = False
    ):
        if state in (EpisodeState.CLOSED, EpisodeState.ARCHIVED):
            await self._conn.execute(
                "UPDATE episodes SET state = ?, end_time = ? WHERE id = ?",
                (state.value, datetime.now(tz=timezone.utc).isoformat(), episode_id),
            )
        else:
            await self._conn.execute(
                "UPDATE episodes SET state = ? WHERE id = ?",
                (state.value, episode_id),
            )
        await self._conn.commit()
        await asyncio.to_thread(
            append_journal,
            self._data_dir,
            episode_id,
            "episode.state_changed",
            {"state": state.value},
        )
        if not _defer_manifest:
            await self.refresh_episode_manifest(episode_id)

    async def update_episode_times(
        self,
        episode_id: str,
        event_time: datetime,
        *,
        activity_time: datetime | None,
        _defer_manifest: bool = False,
    ) -> None:
        event_value = _utc_iso(event_time)
        activity_value = _utc_iso(activity_time)
        await self._conn.execute(
            """UPDATE episodes
               SET last_event_time = CASE
                       WHEN last_event_time IS NULL
                         OR julianday(last_event_time) < julianday(?)
                       THEN ?
                       ELSE last_event_time
                   END,
                   last_activity_at = COALESCE(?, last_activity_at)
               WHERE id = ?""",
            (event_value, event_value, activity_value, episode_id),
        )
        await self._conn.commit()
        if not _defer_manifest:
            await self.refresh_episode_manifest(episode_id)

    async def close_timed_out_episodes(self, timeout: int) -> list[Episode]:
        now = datetime.now(tz=timezone.utc)
        now_value = _utc_iso(now)
        cutoff = _utc_iso(now - timedelta(seconds=timeout))
        rows = await self._conn.execute_fetchall(
            """SELECT * FROM episodes
               WHERE state IN ('active', 'quiescent')
               AND julianday(COALESCE(last_activity_at, last_event_time, start_time))
                   < julianday(?)""",
            (cutoff,),
        )
        closed = []
        for row in rows:
            episode = self._row_to_episode(row)
            cursor = await self._conn.execute(
                """UPDATE episodes
                   SET state = ?, end_time = ?
                   WHERE id = ?
                     AND state IN ('active', 'quiescent')
                     AND julianday(
                         COALESCE(last_activity_at, last_event_time, start_time)
                     ) < julianday(?)""",
                (EpisodeState.CLOSED.value, now_value, episode.id, cutoff),
            )
            await self._conn.commit()
            if cursor.rowcount != 1:
                continue
            await asyncio.to_thread(
                append_journal,
                self._data_dir,
                episode.id,
                "episode.state_changed",
                {"state": EpisodeState.CLOSED.value},
            )
            episode.state = EpisodeState.CLOSED
            episode.end_time = now
            closed.append(episode)
            await self.refresh_episode_manifest(episode.id)
        return closed

    # --- Portable Episode bundles ---

    async def rebuild_episode_manifests(self) -> None:
        await self._bundles.rebuild()

    async def refresh_episode_manifest(self, episode_id: str) -> None:
        await self._bundles.refresh(episode_id)

    # --- Row deserialization ---

    @staticmethod
    def _row_to_evidence(row: aiosqlite.Row) -> Evidence:
        return Evidence(
            id=row["id"],
            device_id=row["device_id"],
            area_id=row["area_id"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            evidence_type=row["evidence_type"],
            file_path=row["file_path"],
            mime_type=row["mime_type"],
            original_filename=row["original_filename"],
            artifact_id=row["artifact_id"],
            byte_size=row["byte_size"],
            sha256=row["sha256"],
            metadata=json.loads(row["metadata"]),
            event_id=row["event_id"],
            episode_id=row["episode_id"],
        )

    @staticmethod
    def _row_to_episode(row: aiosqlite.Row) -> Episode:
        return Episode(
            id=row["id"],
            primary_area_id=row["primary_area_id"],
            start_time=datetime.fromisoformat(row["start_time"]),
            last_event_time=datetime.fromisoformat(row["last_event_time"])
            if row["last_event_time"]
            else None,
            last_activity_at=datetime.fromisoformat(row["last_activity_at"])
            if row["last_activity_at"]
            else None,
            end_time=datetime.fromisoformat(row["end_time"]) if row["end_time"] else None,
            state=EpisodeState(row["state"]),
            event_count=row["event_count"],
            evidence_count=row["evidence_count"],
            summary=row["summary"],
        )
