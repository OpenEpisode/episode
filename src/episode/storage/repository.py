from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from datetime import datetime, timedelta, timezone

import aiosqlite

from episode.config import EpisodeConfig
from episode.domain.models import (
    Area,
    Device,
    Episode,
    EpisodeState,
    Event,
    EventState,
    Evidence,
    IngestionReceipt,
    RawArtifact,
    ReceiptStatus,
    make_event_dedup_key,
)
from episode.storage.bundles import append_journal
from episode.storage.database import SCHEMA_SQL
from episode.storage.files import async_move_to_episode, describe_artifact, move_to_episode
from episode.storage.migrations import (
    migrate_episode_activity_schema,
    migrate_inventory_schema,
    migrate_legacy_identity_schema,
)
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
        self._delivery_conn: aiosqlite.Connection | None = None
        self._delivery_provenance: ProvenanceStore | None = None
        self._delivery_lock = asyncio.Lock()
        self._legacy_identity_schema = False
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
        await migrate_legacy_identity_schema(self._conn)
        await self._conn.executescript(SCHEMA_SQL)
        await migrate_inventory_schema(self._conn)
        await migrate_episode_activity_schema(self._conn)
        event_columns = await self._conn.execute_fetchall("PRAGMA table_info(events)")
        self._legacy_identity_schema = "sensor_id" in {row["name"] for row in event_columns}
        self._provenance = ProvenanceStore(self._conn)
        await self._provenance.migrate_schema()
        await self._conn.commit()
        if not self._legacy_identity_schema:
            await self._enable_foreign_keys(self._conn)
        # Raw artifacts and their receipts use a dedicated connection so their
        # transaction cannot be committed accidentally by another repository
        # coroutine sharing the main connection.
        self._delivery_conn = await aiosqlite.connect(self._db_path)
        self._delivery_conn.row_factory = aiosqlite.Row
        await self._delivery_conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
        await self._delivery_conn.execute("PRAGMA synchronous = NORMAL")
        self._delivery_provenance = ProvenanceStore(self._delivery_conn)
        await self._delivery_provenance.migrate_schema()

        await self._migrate_episode_layout()
        await reconcile_episode_paths(
            self._conn,
            self._provenance,
            self._data_dir,
        )
        await reconcile_episode_counts(self._conn)
        await self._backfill_artifacts()
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

    async def _migrate_episode_layout(self):
        orphan_snaps = os.path.join(self._data_dir, "orphans", "snapshots")
        orphan_payloads = os.path.join(self._data_dir, "orphans", "events")

        # Move old flat evidence/snapshots/ → orphans/snapshots/
        old_snaps = os.path.join(self._data_dir, "evidence", "snapshots")
        if os.path.exists(old_snaps):
            os.makedirs(orphan_snaps, exist_ok=True)
            for fname in os.listdir(old_snaps):
                src = os.path.join(old_snaps, fname)
                if os.path.isfile(src):
                    shutil.move(src, os.path.join(orphan_snaps, fname))
            try:
                os.rmdir(old_snaps)
            except OSError:
                pass

        # Move old evidence/recordings/ → orphans/recordings/ or episode dirs
        old_recs = os.path.join(self._data_dir, "evidence", "recordings")
        if os.path.exists(old_recs):
            os.makedirs(os.path.join(self._data_dir, "orphans", "recordings"), exist_ok=True)
            rows = await self._conn.execute_fetchall(
                "SELECT id, file_path, episode_id FROM evidence WHERE evidence_type = 'recording'"
            )
            for row in rows:
                fp = row["file_path"]
                if fp and os.path.exists(fp) and row["episode_id"]:
                    new_fp = move_to_episode(self._data_dir, row["episode_id"], fp, "recordings")
                    if new_fp != fp:
                        await self._conn.execute(
                            "UPDATE evidence SET file_path = ? WHERE id = ?", (new_fp, row["id"])
                        )
            # Move remaining orphan recordings
            for fname in os.listdir(old_recs):
                src = os.path.join(old_recs, fname)
                if os.path.isfile(src):
                    shutil.move(src, os.path.join(self._data_dir, "orphans", "recordings", fname))
            try:
                os.rmdir(old_recs)
            except OSError:
                pass

        try:
            os.rmdir(os.path.join(self._data_dir, "evidence"))
        except OSError:
            pass

        # Move old events/payloads/ → orphans/payloads/
        old_pl = os.path.join(self._data_dir, "events", "payloads")
        if os.path.exists(old_pl):
            os.makedirs(orphan_payloads, exist_ok=True)
            for fname in os.listdir(old_pl):
                src = os.path.join(old_pl, fname)
                if os.path.isfile(src):
                    shutil.move(src, os.path.join(orphan_payloads, fname))
            try:
                os.rmdir(old_pl)
            except OSError:
                pass
        try:
            os.rmdir(os.path.join(self._data_dir, "events"))
        except OSError:
            pass

        # Relocate all snapshots that have episode_id from orphans/ → episodes/{id}/snapshots/
        rows = await self._conn.execute_fetchall(
            """SELECT id, file_path, episode_id
               FROM evidence
               WHERE evidence_type = 'snapshot' AND episode_id IS NOT NULL"""
        )
        for row in rows:
            fp = row["file_path"]
            if not fp:
                continue
            # file may already be at the right place, or in orphans/, or at the old path
            candidates = [fp]
            basename = os.path.basename(fp)
            candidates.append(os.path.join(orphan_snaps, basename))
            candidates.append(os.path.join(self._data_dir, "evidence", "snapshots", basename))
            source = None
            for c in candidates:
                if os.path.exists(c):
                    source = c
                    break
            if source:
                new_fp = move_to_episode(self._data_dir, row["episode_id"], source, "snapshots")
                if new_fp != fp:
                    await self._conn.execute(
                        "UPDATE evidence SET file_path = ? WHERE id = ?", (new_fp, row["id"])
                    )

        # Relocate all event payloads that have episode_id from orphans/ → episodes/{id}/payloads/
        rows = await self._conn.execute_fetchall(
            """SELECT id, raw_payload_path, episode_id
               FROM events
               WHERE raw_payload_path IS NOT NULL AND episode_id IS NOT NULL"""
        )
        for row in rows:
            fp = row["raw_payload_path"]
            if not fp:
                continue
            candidates = [fp]
            basename = os.path.basename(fp)
            candidates.append(os.path.join(orphan_payloads, basename))
            candidates.append(os.path.join(self._data_dir, "events", "payloads", basename))
            source = None
            for c in candidates:
                if os.path.exists(c):
                    source = c
                    break
            if source:
                new_fp = move_to_episode(self._data_dir, row["episode_id"], source, "events")
                if new_fp != fp:
                    await self._conn.execute(
                        "UPDATE events SET raw_payload_path = ? WHERE id = ?", (new_fp, row["id"])
                    )

        await self._conn.commit()

        # Clean up empty leftover payloads/ dirs
        for d in [os.path.join(self._data_dir, "orphans", "payloads")]:
            if os.path.isdir(d):
                try:
                    os.rmdir(d)
                except OSError:
                    pass
        episodes_dir = os.path.join(self._data_dir, "episodes")
        if os.path.exists(episodes_dir):
            for ep_id in os.listdir(episodes_dir):
                d = os.path.join(episodes_dir, ep_id, "payloads")
                if os.path.isdir(d):
                    try:
                        os.rmdir(d)
                    except OSError:
                        pass

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
        limit: int = 200,
    ) -> list[IngestionReceipt]:
        if self._provenance is None:
            raise RuntimeError("Repository is not initialized")
        return await self._provenance.list_receipts(
            episode_id=episode_id,
            event_id=event_id,
            evidence_id=evidence_id,
            limit=limit,
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

    async def _backfill_artifacts(self) -> None:
        event_rows = await self._conn.execute_fetchall(
            """SELECT * FROM events
               WHERE raw_payload_path IS NOT NULL AND raw_payload_path != ''"""
        )
        for row in event_rows:
            artifact = await self._describe_existing_artifact(
                row["raw_payload_path"], "event_payload", "application/xml"
            )
            if not artifact:
                continue
            receipts = await self.list_ingestion_receipts(event_id=row["id"], limit=1)
            if not receipts:
                await self.create_ingestion_receipt(
                    IngestionReceipt(
                        source=row["source"] or "legacy:event",
                        received_at=datetime.fromisoformat(row["timestamp"]),
                        observed_at=datetime.fromisoformat(row["timestamp"]),
                        artifact_id=artifact.id,
                        device_id=row["device_id"],
                        area_id=row["area_id"],
                        event_id=row["id"],
                        episode_id=row["episode_id"],
                        metadata={"migrated": True},
                    )
                )

        evidence_rows = await self._conn.execute_fetchall("SELECT * FROM evidence")
        for row in evidence_rows:
            if row["artifact_id"]:
                continue
            artifact = await self._describe_existing_artifact(
                row["file_path"],
                row["evidence_type"] or "evidence",
                row["mime_type"] or "application/octet-stream",
                row["original_filename"],
            )
            if not artifact:
                continue
            await self._conn.execute(
                """UPDATE evidence
                   SET artifact_id = ?, byte_size = ?, sha256 = ? WHERE id = ?""",
                (artifact.id, artifact.byte_size, artifact.sha256, row["id"]),
            )
            metadata = json.loads(row["metadata"])
            if metadata.get("origin") == "ftp":
                receipts = await self.list_ingestion_receipts(evidence_id=row["id"], limit=1)
                if not receipts:
                    await self.create_ingestion_receipt(
                        IngestionReceipt(
                            source="hikvision:ftp",
                            received_at=datetime.fromisoformat(row["timestamp"]),
                            observed_at=datetime.fromisoformat(row["timestamp"]),
                            artifact_id=artifact.id,
                            device_id=row["device_id"],
                            area_id=row["area_id"],
                            evidence_id=row["id"],
                            episode_id=row["episode_id"],
                            metadata={"migrated": True},
                        )
                    )
        await self._conn.commit()

    # --- Areas ---

    async def upsert_area(self, area: Area) -> Area:
        await self._conn.execute(
            """INSERT INTO areas (id, name, location, metadata, enabled)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   name=excluded.name,
                   location=excluded.location,
                   metadata=excluded.metadata,
                   enabled=excluded.enabled""",
            (
                area.id,
                area.name,
                area.location,
                json.dumps(area.metadata),
                int(area.enabled),
            ),
        )
        await self._conn.commit()
        return area

    async def get_area(self, area_id: str) -> Area | None:
        row = await self._conn.execute_fetchall("SELECT * FROM areas WHERE id = ?", (area_id,))
        if not row:
            return None
        return self._row_to_area(row[0])

    async def list_areas(self, *, include_disabled: bool = False) -> list[Area]:
        query = "SELECT * FROM areas"
        if not include_disabled:
            query += " WHERE enabled = 1"
        rows = await self._conn.execute_fetchall(query + " ORDER BY name")
        return [self._row_to_area(row) for row in rows]

    async def delete_area(self, area_id: str):
        await self._conn.execute("DELETE FROM areas WHERE id = ?", (area_id,))
        await self._conn.commit()

    # --- Devices ---

    async def upsert_device(self, device: Device) -> Device:
        await self._conn.execute(
            """INSERT INTO devices (
                id, name, device_type, area_id,
                capabilities, ip_address, username, password,
                configs, metadata, enabled
            )
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   name=excluded.name,
                   device_type=excluded.device_type,
                   area_id=excluded.area_id,
                   capabilities=excluded.capabilities,
                   ip_address=excluded.ip_address,
                   username=excluded.username,
                   password=excluded.password,
                   configs=excluded.configs,
                   metadata=excluded.metadata,
                   enabled=excluded.enabled""",
            (
                device.id,
                device.name,
                device.device_type,
                device.area_id,
                json.dumps(device.capabilities),
                device.ip_address,
                device.username,
                device.password,
                json.dumps(
                    {
                        key: {
                            "protocol": value.protocol,
                            "port": value.port,
                            "path": value.path,
                            "settings": value.settings,
                        }
                        for key, value in device.configs.items()
                    }
                ),
                json.dumps(device.metadata),
                int(device.enabled),
            ),
        )
        await self._conn.commit()
        return device

    async def get_device(self, device_id: str) -> Device | None:
        row = await self._conn.execute_fetchall("SELECT * FROM devices WHERE id = ?", (device_id,))
        if not row:
            return None
        return self._row_to_device(row[0])

    async def find_device_by_ip(self, ip_address: str) -> Device | None:
        rows = await self._conn.execute_fetchall(
            "SELECT * FROM devices WHERE ip_address = ?",
            (ip_address,),
        )
        return self._row_to_device(rows[0]) if rows else None

    async def list_devices(
        self,
        area_id: str | None = None,
        *,
        include_disabled: bool = False,
    ) -> list[Device]:
        clauses = []
        params = []
        if area_id:
            clauses.append("area_id = ?")
            params.append(area_id)
        if not include_disabled:
            clauses.append("enabled = 1")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = await self._conn.execute_fetchall(
            f"SELECT * FROM devices{where} ORDER BY name",
            params,
        )
        return [self._row_to_device(row) for row in rows]

    async def delete_device(self, device_id: str):
        await self._conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))
        await self._conn.commit()

    async def get_setting(self, key: str) -> str | None:
        rows = await self._conn.execute_fetchall(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        )
        return rows[0]["value"] if rows else None

    async def set_setting(self, key: str, value: str) -> None:
        await self._conn.execute(
            """INSERT INTO app_settings (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (key, value),
        )
        await self._conn.commit()

    async def area_usage(self, area_id: str) -> dict[str, int]:
        row = (
            await self._conn.execute_fetchall(
                """SELECT
                    (SELECT COUNT(*) FROM devices WHERE area_id = ?) AS devices,
                    (SELECT COUNT(*) FROM episodes WHERE primary_area_id = ?) AS episodes,
                    (SELECT COUNT(*) FROM events WHERE area_id = ?) AS events,
                    (SELECT COUNT(*) FROM evidence WHERE area_id = ?) AS evidence,
                    (SELECT COUNT(*) FROM ingestion_receipts WHERE area_id = ?) AS receipts""",
                (area_id, area_id, area_id, area_id, area_id),
            )
        )[0]
        return {key: int(row[key]) for key in row.keys()}

    async def device_usage(self, device_id: str) -> dict[str, int]:
        row = (
            await self._conn.execute_fetchall(
                """SELECT
                    (SELECT COUNT(*) FROM events WHERE device_id = ?) AS events,
                    (SELECT COUNT(*) FROM evidence WHERE device_id = ?) AS evidence,
                    (SELECT COUNT(*) FROM ingestion_receipts WHERE device_id = ?) AS receipts""",
                (device_id, device_id, device_id),
            )
        )[0]
        return {key: int(row[key]) for key in row.keys()}

    # --- Events ---

    async def create_event(self, event: Event) -> Event:
        if not event.dedup_key:
            event.dedup_key = make_event_dedup_key(
                event.device_id, event.timestamp, event.event_type, event.event_state
            )
        if self._legacy_identity_schema:
            await self._conn.execute(
                """INSERT INTO events (
                    id, device_id, area_id, sensor_id, asset_id, timestamp,
                    event_type, event_state, source, dedup_key,
                    raw_payload_path, metadata, episode_id
                )
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event.id,
                    event.device_id,
                    event.area_id,
                    event.device_id,
                    event.area_id,
                    _utc_iso(event.timestamp),
                    event.event_type,
                    event.event_state.value,
                    event.source,
                    event.dedup_key,
                    event.raw_payload_path,
                    json.dumps(event.metadata),
                    event.episode_id,
                ),
            )
        else:
            await self._conn.execute(
                """INSERT INTO events (
                    id, device_id, area_id, timestamp,
                    event_type, event_state, source, dedup_key,
                    raw_payload_path, metadata, episode_id
                )
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event.id,
                    event.device_id,
                    event.area_id,
                    _utc_iso(event.timestamp),
                    event.event_type,
                    event.event_state.value,
                    event.source,
                    event.dedup_key,
                    event.raw_payload_path,
                    json.dumps(event.metadata),
                    event.episode_id,
                ),
            )
        await self._conn.commit()
        return event

    async def canonicalize_event(self, event: Event) -> tuple[Event, bool]:
        if not event.dedup_key:
            event.dedup_key = make_event_dedup_key(
                event.device_id, event.timestamp, event.event_type, event.event_state
            )
        existing = await self.find_event_by_dedup_key(event.dedup_key)
        if existing:
            return existing, False
        try:
            return await self.create_event(event), True
        except aiosqlite.IntegrityError:
            existing = await self.find_event_by_dedup_key(event.dedup_key)
            if existing:
                return existing, False
            raise

    async def get_event(self, event_id: str) -> Event | None:
        row = await self._conn.execute_fetchall("SELECT * FROM events WHERE id = ?", (event_id,))
        if not row:
            return None
        return self._row_to_event(row[0])

    async def find_event_by_dedup_key(self, dedup_key: str) -> Event | None:
        rows = await self._conn.execute_fetchall(
            "SELECT * FROM events WHERE dedup_key = ? LIMIT 1", (dedup_key,)
        )
        return self._row_to_event(rows[0]) if rows else None

    async def list_events(
        self,
        episode_id: str | None = None,
        area_id: str | None = None,
        device_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Event]:
        clauses = []
        params = []
        if episode_id:
            clauses.append("episode_id = ?")
            params.append(episode_id)
        if area_id:
            clauses.append("area_id = ?")
            params.append(area_id)
        if device_id:
            clauses.append("device_id = ?")
            params.append(device_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = await self._conn.execute_fetchall(
            f"SELECT * FROM events{where} ORDER BY timestamp DESC, id DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        return [self._row_to_event(r) for r in rows]

    async def find_recent_events_by_device(self, device_id: str, since: datetime) -> list[Event]:
        rows = await self._conn.execute_fetchall(
            "SELECT * FROM events WHERE device_id = ? AND timestamp >= ? ORDER BY timestamp ASC",
            (device_id, since.isoformat()),
        )
        return [self._row_to_event(r) for r in rows]

    async def update_event_episode(self, event_id: str, episode_id: str):
        await self._conn.execute(
            "UPDATE events SET episode_id = ? WHERE id = ?",
            (episode_id, event_id),
        )
        await self._conn.commit()

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

        if self._legacy_identity_schema:
            await self._conn.execute(
                """INSERT INTO evidence (
                    id, device_id, area_id, sensor_id, asset_id, timestamp,
                    evidence_type, file_path, mime_type, original_filename,
                    artifact_id, byte_size, sha256, metadata, event_id, episode_id
                )
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    evidence.id,
                    evidence.device_id,
                    evidence.area_id,
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
        else:
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
        if self._legacy_identity_schema:
            await self._conn.execute(
                """INSERT INTO episodes (
                    id, primary_area_id, primary_asset_id, start_time,
                    last_event_time, last_activity_at, end_time, state,
                    event_count, evidence_count, summary
                )
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    episode.id,
                    episode.primary_area_id,
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
        else:
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
    def _row_to_area(row: aiosqlite.Row) -> Area:
        return Area(
            id=row["id"],
            name=row["name"],
            location=row["location"],
            metadata=json.loads(row["metadata"]),
            enabled=bool(row["enabled"]),
        )

    @staticmethod
    def _row_to_device(row: aiosqlite.Row) -> Device:
        return Device(
            id=row["id"],
            name=row["name"],
            device_type=row["device_type"],
            area_id=row["area_id"],
            capabilities=json.loads(row["capabilities"]) if row["capabilities"] else [],
            ip_address=row["ip_address"],
            username=row["username"],
            password=row["password"],
            configs=json.loads(row["configs"]) if row["configs"] else {},
            metadata=json.loads(row["metadata"]),
            enabled=bool(row["enabled"]),
        )

    @staticmethod
    def _row_to_event(row: aiosqlite.Row) -> Event:
        return Event(
            id=row["id"],
            device_id=row["device_id"],
            area_id=row["area_id"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            event_type=row["event_type"],
            event_state=EventState(row["event_state"]),
            source=row["source"],
            dedup_key=row["dedup_key"] or "",
            raw_payload_path=row["raw_payload_path"],
            metadata=json.loads(row["metadata"]),
            episode_id=row["episode_id"],
        )

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
