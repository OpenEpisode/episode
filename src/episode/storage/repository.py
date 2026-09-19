from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import aiosqlite

from episode.config import EpisodeConfig
from episode.domain.event_filter import LEGACY_FILTER_SELECTOR_JSON
from episode.domain.lifecycle import (
    DEFAULT_QUIESCENT_GRACE_SECONDS,
    QUIESCENT_GRACE_SETTING,
    validate_quiescent_grace_seconds,
)
from episode.domain.models import (
    Area,
    CaptureProfile,
    CaptureProfileChange,
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
from episode.storage.capture_profiles import CaptureProfileStore
from episode.storage.database import SCHEMA_SQL
from episode.storage.events import EventStore
from episode.storage.files import async_move_to_episode, describe_artifact
from episode.storage.inventory import InventoryStore
from episode.storage.projection import EpisodeBundleProjector
from episode.storage.provenance import ProvenanceStore
from episode.storage.recovery import reconcile_episode_counts, reconcile_episode_paths

logger = logging.getLogger(__name__)


SQLITE_BUSY_TIMEOUT_MS = 15_000

_BETA6_EVENT_COLUMNS = {
    "id",
    "device_id",
    "area_id",
    "timestamp",
    "event_type",
    "event_state",
    "source",
    "dedup_key",
    "raw_payload_path",
    "metadata",
    "episode_id",
}

_CAPTURE_PROFILE_EVENT_COLUMNS = {
    "participation": "TEXT",
    "eligible_recording_device_ids": "TEXT",
}

# Additive capture-policy columns for existing databases. ``CREATE TABLE IF NOT
# EXISTS`` does not alter existing tables, so these are applied via idempotent
# ``ALTER TABLE ADD COLUMN`` guarded by a column-existence check.
_CAPTURE_POLICY_DEVICE_COLUMNS = {
    "event_filter": "TEXT NOT NULL DEFAULT 'inherit'",
}

_CAPTURE_POLICY_PROFILE_COLUMNS = {
    "event_filter": "TEXT NOT NULL DEFAULT '[]'",
}

# ``0.1.0-beta.7`` shipped a profile boolean and a Device tri-state. Their class
# equivalents are backfilled below and the columns are then dropped, so no
# legacy keyword survives in the schema or in stored values.
_LEGACY_DEVICE_FILTER_COLUMN = "generic_event_filter"
_LEGACY_PROFILE_FILTER_COLUMN = "filter_generic_events"

_MIN_DROP_COLUMN_SQLITE = (3, 35, 0)

_EVIDENCE_SELECT = """
SELECT e.*,
       x.expired_at AS retention_expired_at,
       x.reason AS retention_expiration_reason
FROM evidence e
LEFT JOIN evidence_expirations x ON x.evidence_id = e.id
"""


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
        self._capture_profile_conn: aiosqlite.Connection | None = None
        self._capture_profiles: CaptureProfileStore | None = None
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
        await self._upgrade_event_schema(self._conn)
        await self._conn.executescript(SCHEMA_SQL)
        await self._upgrade_capture_policy_schema(self._conn)
        self._provenance = ProvenanceStore(self._conn)
        self._inventory = InventoryStore(self._conn)
        self._events = EventStore(self._conn)
        await self._conn.commit()
        await self._enable_foreign_keys(self._conn)

        # Capture-profile activation uses an explicit SQLite transaction. Keep
        # it on its own connection so unrelated Event or inventory commits
        # cannot accidentally split the active-pointer and audit-row update.
        self._capture_profile_conn = await aiosqlite.connect(self._db_path)
        self._capture_profile_conn.row_factory = aiosqlite.Row
        await self._capture_profile_conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
        await self._capture_profile_conn.execute("PRAGMA synchronous = NORMAL")
        await self._enable_foreign_keys(self._capture_profile_conn)
        self._capture_profiles = CaptureProfileStore(self._capture_profile_conn)
        await self._capture_profiles.ensure_defaults()
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

    async def _upgrade_event_schema(self, connection: aiosqlite.Connection) -> None:
        """Apply the bounded additive Beta.6 to Beta.7 Event schema step."""
        tables = await connection.execute_fetchall(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'events'"
        )
        if not tables:
            return
        columns = {
            str(row["name"])
            for row in await connection.execute_fetchall("PRAGMA table_info(events)")
        }
        if not _BETA6_EVENT_COLUMNS.issubset(columns):
            await connection.close()
            self._conn = None
            raise RuntimeError(
                "The Episode database uses an unsupported pre-release schema; "
                "start this release with a clean data directory"
            )

        missing = [
            (name, column_type)
            for name, column_type in _CAPTURE_PROFILE_EVENT_COLUMNS.items()
            if name not in columns
        ]
        if not missing:
            return
        try:
            await connection.execute("BEGIN IMMEDIATE")
            for name, column_type in missing:
                await connection.execute(f"ALTER TABLE events ADD COLUMN {name} {column_type}")
            await connection.commit()
            logger.info(
                "Applied additive Event schema step for columns: %s",
                ", ".join(name for name, _ in missing),
            )
        except BaseException:
            await connection.rollback()
            raise

    async def _upgrade_capture_policy_schema(self, connection: aiosqlite.Connection) -> None:
        """Replace the ``beta.7`` filter columns with event-class selectors.

        One bounded, idempotent step per table: add the class-selector column,
        backfill it from the legacy boolean/tri-state when that column exists,
        then drop the legacy column. No rows are deleted and no legacy keyword
        survives, so repeated ``initialize()`` calls converge on the same shape.
        """
        tables = {
            str(row["name"])
            for row in await connection.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name IN ('devices', 'capture_profiles')"
            )
        }
        add_steps: list[tuple[str, str, str]] = []
        migrate_steps: list[tuple[str, str]] = []
        for table, legacy_column, column_types in (
            ("devices", _LEGACY_DEVICE_FILTER_COLUMN, _CAPTURE_POLICY_DEVICE_COLUMNS),
            ("capture_profiles", _LEGACY_PROFILE_FILTER_COLUMN, _CAPTURE_POLICY_PROFILE_COLUMNS),
        ):
            if table not in tables:
                continue
            columns = {
                str(row["name"])
                for row in await connection.execute_fetchall(f"PRAGMA table_info({table})")
            }
            add_steps.extend(
                (table, name, column_type)
                for name, column_type in column_types.items()
                if name not in columns
            )
            if legacy_column in columns:
                migrate_steps.append((table, legacy_column))
        if not add_steps and not migrate_steps:
            return

        try:
            await connection.execute("BEGIN IMMEDIATE")
            for table, name, column_type in add_steps:
                await connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {column_type}")
            if ("capture_profiles", _LEGACY_PROFILE_FILTER_COLUMN) in migrate_steps:
                await connection.execute(
                    """UPDATE capture_profiles
                       SET event_filter = CASE
                           WHEN filter_generic_events = 1 THEN ?
                           ELSE '[]'
                       END""",
                    (LEGACY_FILTER_SELECTOR_JSON,),
                )
            if ("devices", _LEGACY_DEVICE_FILTER_COLUMN) in migrate_steps:
                await connection.execute(
                    """UPDATE devices
                       SET event_filter = CASE generic_event_filter
                           WHEN 'enabled' THEN ?
                           WHEN 'disabled' THEN '[]'
                           ELSE 'inherit'
                       END""",
                    (LEGACY_FILTER_SELECTOR_JSON,),
                )
            for table, legacy_column in migrate_steps:
                await self._drop_column(connection, table, legacy_column)
            await connection.commit()
            logger.info(
                "Applied capture-policy schema steps: %s",
                ", ".join(
                    [f"add {table}.{name}" for table, name, _ in add_steps]
                    + [f"replace {table}.{legacy}" for table, legacy in migrate_steps]
                ),
            )
        except BaseException:
            await connection.rollback()
            raise

    @staticmethod
    async def _drop_column(
        connection: aiosqlite.Connection,
        table: str,
        column: str,
    ) -> None:
        """Drop a legacy column, refusing to leave the two representations coexisting."""
        version = tuple(int(part) for part in sqlite3.sqlite_version.split(".")[:3])
        if version < _MIN_DROP_COLUMN_SQLITE:
            raise RuntimeError(
                f"Episode requires SQLite {'.'.join(str(part) for part in _MIN_DROP_COLUMN_SQLITE)}"
                " or newer for ALTER TABLE DROP COLUMN to replace "
                f"{table}.{column}; found {sqlite3.sqlite_version}"
            )
        await connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")

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
        if self._capture_profile_conn:
            await self._capture_profile_conn.close()
            self._capture_profile_conn = None
            self._capture_profiles = None
        if self._conn:
            await self._conn.close()
            self._conn = None
            self._provenance = None
            self._inventory = None
            self._events = None

    async def get_system_setting(self, key: str) -> str | None:
        rows = await self._conn.execute_fetchall(
            "SELECT value FROM system_settings WHERE key = ?",
            (key,),
        )
        return str(rows[0]["value"]) if rows else None

    async def set_system_setting(self, key: str, value: str) -> None:
        await self._conn.execute(
            """INSERT INTO system_settings (key, value, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE
               SET value = excluded.value, updated_at = excluded.updated_at""",
            (key, value, _utc_iso(datetime.now(tz=timezone.utc))),
        )
        await self._conn.commit()

    async def get_quiescent_grace_seconds(
        self,
        default: int = DEFAULT_QUIESCENT_GRACE_SECONDS,
    ) -> int:
        """Return the persisted Episode settling window or its safe default."""
        fallback = validate_quiescent_grace_seconds(default)
        value = await self.get_system_setting(QUIESCENT_GRACE_SETTING)
        if value is None:
            return fallback
        try:
            parsed = int(value)
            return validate_quiescent_grace_seconds(parsed)
        except (TypeError, ValueError):
            logger.error("Invalid stored Episode quiescent grace; using default")
            return fallback

    async def _mark_raw_artifact_expired(self, artifact_id: str, expired_at: str) -> None:
        rows = await self._conn.execute_fetchall(
            "SELECT metadata FROM raw_artifacts WHERE id = ?",
            (artifact_id,),
        )
        if not rows:
            return
        try:
            metadata = json.loads(rows[0]["metadata"] or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        metadata.update(
            {
                "availability": "expired",
                "expired_at": expired_at,
                "expiration_reason": "retention_policy",
            }
        )
        await self._conn.execute(
            "UPDATE raw_artifacts SET file_path = ?, metadata = ? WHERE id = ?",
            (f"expired:{artifact_id}", json.dumps(metadata), artifact_id),
        )

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

    # --- Capture profiles ---

    def _capture_profile_store(self) -> CaptureProfileStore:
        if self._capture_profiles is None:
            raise RuntimeError("Repository is not initialized")
        return self._capture_profiles

    async def list_capture_profiles(self) -> list[CaptureProfile]:
        return await self._capture_profile_store().list()

    async def get_capture_profile(self, profile_id: str) -> CaptureProfile | None:
        return await self._capture_profile_store().get(profile_id)

    async def get_active_capture_profile(self) -> CaptureProfile | None:
        return await self._capture_profile_store().active()

    async def create_capture_profile(self, profile: CaptureProfile) -> CaptureProfile:
        return await self._capture_profile_store().create(profile)

    async def update_capture_profile(self, profile: CaptureProfile) -> CaptureProfile:
        return await self._capture_profile_store().update(profile)

    async def delete_capture_profile(self, profile_id: str) -> None:
        await self._capture_profile_store().delete(profile_id)

    async def activate_capture_profile(
        self, profile_id: str, *, source: str
    ) -> CaptureProfileChange:
        return await self._capture_profile_store().activate(profile_id, source)

    async def list_capture_profile_changes(
        self, *, limit: int = 20, offset: int = 0
    ) -> list[CaptureProfileChange]:
        return await self._capture_profile_store().list_changes(limit=limit, offset=offset)

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

    async def find_preceding_event_transition(self, event: Event) -> Event | None:
        return await self._event_store().find_preceding_transition(event)

    async def update_event_episode(self, event_id: str, episode_id: str) -> None:
        await self._event_store().update_episode(event_id, episode_id)

    async def update_event_participation(self, event: Event) -> None:
        """Re-persist an Event's participation decision in place.

        The engine learns some outcomes only after canonicalization, such as
        whether an Episode was open to attach a filtered Event to. The decision
        is derived data recorded on the Event row; the canonical observation
        itself is never rewritten.
        """
        await self._event_store().update_participation(event.id, event.participation)

    async def visual_artifacts_for_event(self, event_id: str) -> list[RawArtifact]:
        rows = await self._conn.execute_fetchall(
            """SELECT DISTINCT a.*
               FROM raw_artifacts a
               WHERE a.id IN (
                   SELECT artifact_id FROM ingestion_receipts
                   WHERE event_id = ? AND artifact_id IS NOT NULL
               )""",
            (event_id,),
        )
        return [self._provenance._row_to_artifact(row) for row in rows]

    async def mark_event_visual_expired(
        self,
        event: Event,
        *,
        expired_at: datetime,
        artifact_ids: list[str],
    ) -> None:
        expired_value = _utc_iso(expired_at)
        metadata = dict(event.metadata)
        metadata.pop("embedded_picture", None)
        metadata.pop("picture_sha256", None)
        metadata["visual_evidence"] = {
            "availability": "expired",
            "expired_at": expired_value,
            "expiration_reason": "retention_policy",
        }
        await self._conn.execute(
            "UPDATE events SET raw_payload_path = NULL, metadata = ? WHERE id = ?",
            (json.dumps(metadata), event.id),
        )
        for artifact_id in artifact_ids:
            references = await self._conn.execute_fetchall(
                """SELECT 1 FROM evidence e
                   LEFT JOIN evidence_expirations x ON x.evidence_id = e.id
                   WHERE e.artifact_id = ? AND x.evidence_id IS NULL
                   UNION ALL
                   SELECT 1 FROM ingestion_receipts r
                   JOIN events linked ON linked.id = r.event_id
                   WHERE r.artifact_id = ? AND linked.id != ?
                     AND linked.raw_payload_path IS NOT NULL
                   LIMIT 1""",
                (artifact_id, artifact_id, event.id),
            )
            if not references:
                await self._mark_raw_artifact_expired(artifact_id, expired_value)
        await self._conn.commit()
        if event.episode_id:
            await self.append_episode_journal(
                event.episode_id,
                "event.visual_evidence_expired",
                {
                    "event_id": event.id,
                    "captured_at": event.timestamp.isoformat(),
                    "reason": "retention_policy",
                },
            )
            await self.refresh_episode_manifest(event.episode_id)

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
                if evidence.metadata.get("format") == "hls-fmp4":
                    evidence.byte_size = evidence.metadata.get("bundle_bytes", artifact.byte_size)
                    evidence.sha256 = evidence.metadata.get(
                        "component_manifest_sha256", artifact.sha256
                    )
                else:
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
            f"{_EVIDENCE_SELECT} WHERE e.id = ?",
            (evidence_id,),
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
        available_only: bool | None = None,
    ) -> list[Evidence]:
        clauses = []
        params = []
        if episode_id:
            clauses.append("e.episode_id = ?")
            params.append(episode_id)
        if event_id:
            clauses.append("e.event_id = ?")
            params.append(event_id)
        if device_id:
            clauses.append("e.device_id = ?")
            params.append(device_id)
        if area_id:
            clauses.append("e.area_id = ?")
            params.append(area_id)
        if evidence_type:
            clauses.append("e.evidence_type = ?")
            params.append(evidence_type)
        if has_episode is not None:
            clauses.append("e.episode_id IS NOT NULL" if has_episode else "e.episode_id IS NULL")
        if available_only is not None:
            clauses.append(
                "x.evidence_id IS NULL" if available_only else "x.evidence_id IS NOT NULL"
            )
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = await self._conn.execute_fetchall(
            f"{_EVIDENCE_SELECT}{where} ORDER BY e.timestamp DESC, e.id DESC LIMIT ? OFFSET ?",
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
                  FROM evidence candidate
                  LEFT JOIN evidence_expirations x ON x.evidence_id = candidate.id
                  WHERE candidate.episode_id IN ({placeholders})
                    AND candidate.mime_type LIKE 'image/%'
                    AND x.evidence_id IS NULL
                  GROUP BY candidate.episode_id
                ) first_image
                  ON e.episode_id = first_image.episode_id
                 AND e.timestamp = first_image.min_ts
                LEFT JOIN evidence_expirations expired ON expired.evidence_id = e.id
                WHERE e.mime_type LIKE 'image/%' AND expired.evidence_id IS NULL""",
            episode_ids,
        )
        return {row["episode_id"]: row["evidence_id"] for row in rows}

    async def find_orphan_evidence(self, older_than: timedelta | None = None) -> list[Evidence]:
        rows = await self._conn.execute_fetchall(
            f"{_EVIDENCE_SELECT} "
            "WHERE e.event_id IS NULL AND x.evidence_id IS NULL "
            "ORDER BY e.timestamp ASC"
        )
        return [self._row_to_evidence(r) for r in rows]

    async def find_orphan_evidence_by_device(self, device_id: str) -> list[Evidence]:
        rows = await self._conn.execute_fetchall(
            f"{_EVIDENCE_SELECT} "
            "WHERE e.device_id = ? AND e.event_id IS NULL AND e.episode_id IS NULL "
            "AND x.evidence_id IS NULL ORDER BY e.timestamp ASC",
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

    async def list_visual_evidence_before(self, cutoff: datetime) -> list[Evidence]:
        rows = await self._conn.execute_fetchall(
            f"{_EVIDENCE_SELECT} "
            "WHERE x.evidence_id IS NULL AND e.timestamp < ? "
            "AND (e.mime_type LIKE 'image/%' OR e.mime_type LIKE 'video/%' "
            "OR e.evidence_type IN ('recording', 'incomplete_recording')) "
            "ORDER BY e.timestamp ASC",
            (_utc_iso(cutoff),),
        )
        return [self._row_to_evidence(row) for row in rows]

    async def visual_artifacts_for_evidence(self, evidence_id: str) -> list[RawArtifact]:
        rows = await self._conn.execute_fetchall(
            """SELECT DISTINCT a.*
               FROM raw_artifacts a
               WHERE a.id = (SELECT artifact_id FROM evidence WHERE id = ?)
                  OR a.id IN (
                      SELECT artifact_id FROM ingestion_receipts
                      WHERE evidence_id = ? AND artifact_id IS NOT NULL
                  )""",
            (evidence_id, evidence_id),
        )
        return [self._provenance._row_to_artifact(row) for row in rows]

    async def mark_evidence_expired(
        self,
        evidence: Evidence,
        *,
        expired_at: datetime,
        artifact_ids: list[str],
    ) -> None:
        expired_value = _utc_iso(expired_at)
        await self._conn.execute(
            """INSERT OR IGNORE INTO evidence_expirations
               (evidence_id, expired_at, reason) VALUES (?, ?, ?)""",
            (evidence.id, expired_value, "retention_policy"),
        )
        await self._conn.execute(
            """UPDATE evidence
               SET file_path = ''
               WHERE id = ?""",
            (evidence.id,),
        )
        for artifact_id in artifact_ids:
            references = await self._conn.execute_fetchall(
                """SELECT 1
                   FROM evidence e
                   LEFT JOIN evidence_expirations x ON x.evidence_id = e.id
                   WHERE e.artifact_id = ? AND e.id != ? AND x.evidence_id IS NULL
                   LIMIT 1""",
                (artifact_id, evidence.id),
            )
            if not references:
                await self._mark_raw_artifact_expired(artifact_id, expired_value)
        await self._conn.commit()

        if evidence.episode_id:
            await self.append_episode_journal(
                evidence.episode_id,
                "evidence.expired",
                {
                    "evidence_id": evidence.id,
                    "evidence_type": evidence.evidence_type,
                    "captured_at": evidence.timestamp.isoformat(),
                    "sha256": evidence.sha256,
                    "reason": "retention_policy",
                },
            )
            await self.refresh_episode_manifest(evidence.episode_id)

    # --- Episodes ---

    async def create_episode(self, episode: Episode) -> Episode:
        await self._conn.execute(
            """INSERT INTO episodes (
                id, primary_area_id, start_time,
                last_event_time, last_activity_at, minimum_end_at, end_time, state,
                event_count, evidence_count, summary
            )
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                episode.id,
                episode.primary_area_id,
                _utc_iso(episode.start_time),
                _utc_iso(episode.last_event_time),
                _utc_iso(episode.last_activity_at),
                _utc_iso(episode.minimum_end_at),
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

    async def find_open_episode_for_area(
        self,
        area_id: str,
        timeout: int,
        *,
        at: datetime | None = None,
        quiescent_grace_seconds: int = DEFAULT_QUIESCENT_GRACE_SECONDS,
    ) -> Episode | None:
        """Find an Area Episode open at the supplied ingress time.

        ``at`` is deliberately supplied by the caller.  Using query time here
        makes an Event received before a deadline look late when persistence is
        delayed by another delivery or a busy database.
        """
        reference = at or datetime.now(tz=timezone.utc)
        grace = validate_quiescent_grace_seconds(quiescent_grace_seconds)
        grace_cutoff = _utc_iso(reference - timedelta(seconds=grace))
        fallback_cutoff = _utc_iso(reference - timedelta(seconds=timeout + grace))
        rows = await self._conn.execute_fetchall(
            """SELECT * FROM episodes
               WHERE primary_area_id = ?
               AND state IN ('active', 'quiescent')
               AND (
                   (minimum_end_at IS NOT NULL
                    AND julianday(minimum_end_at) >= julianday(?))
                   OR
                   (minimum_end_at IS NULL
                    AND julianday(COALESCE(last_activity_at, last_event_time, start_time))
                        >= julianday(?))
               )
               ORDER BY julianday(
                   COALESCE(last_activity_at, last_event_time, start_time)
               ) DESC
               LIMIT 1""",
            (area_id, grace_cutoff, fallback_cutoff),
        )
        episode = self._row_to_episode(rows[0]) if rows else None
        if episode:
            deadline = episode.minimum_end_at
            if deadline is None:
                baseline = episode.last_activity_at or episode.last_event_time or episode.start_time
                deadline = baseline + timedelta(seconds=timeout)
            if reference > deadline + timedelta(seconds=grace):
                episode = None
        logger.debug(
            "Open episode for area %s: %s (at=%s, grace=%ss, activity=%s)",
            area_id,
            episode.id if episode else None,
            reference,
            grace,
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
            subdir = (
                "recordings"
                if evidence.metadata.get("format") == "hls-fmp4"
                else {
                    "snapshot": "snapshots",
                    "recording": "recordings",
                }.get(evidence.evidence_type, "other")
            )
            expected_root = os.path.abspath(
                os.path.join(self._data_dir, "episodes", episode_id, subdir)
            )
            existing_path = os.path.abspath(evidence.file_path)
            is_managed_bundle = evidence.metadata.get("format") == "hls-fmp4" and (
                existing_path == expected_root or existing_path.startswith(expected_root + os.sep)
            )
            if not is_managed_bundle:
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

    async def extend_episode_minimum_end(
        self,
        episode_id: str,
        minimum_end_at: datetime,
        *,
        _defer_manifest: bool = False,
    ) -> None:
        value = _utc_iso(minimum_end_at)
        await self._conn.execute(
            """UPDATE episodes
               SET minimum_end_at = CASE
                   WHEN minimum_end_at IS NULL
                     OR julianday(minimum_end_at) < julianday(?)
                   THEN ?
                   ELSE minimum_end_at
               END
               WHERE id = ?""",
            (value, value, episode_id),
        )
        await self._conn.commit()
        if not _defer_manifest:
            await self.refresh_episode_manifest(episode_id)

    async def transition_timed_out_episodes(
        self,
        timeout: int,
        quiescent_grace_seconds: int = DEFAULT_QUIESCENT_GRACE_SECONDS,
        *,
        now: datetime | None = None,
    ) -> tuple[list[Episode], list[Episode]]:
        """Move expired Episodes through quiescence and then close them.

        The returned lists contain Episodes transitioned to QUIESCENT and
        CLOSED respectively.  ``now`` is injectable so lifecycle decisions can
        be tested against the same clock as Event ingress.
        """
        observed_at = now or datetime.now(tz=timezone.utc)
        grace = validate_quiescent_grace_seconds(quiescent_grace_seconds)
        rows = await self._conn.execute_fetchall(
            """SELECT * FROM episodes
               WHERE state IN ('active', 'quiescent')"""
        )
        quiescent: list[Episode] = []
        closed: list[Episode] = []
        for row in rows:
            episode = self._row_to_episode(row)
            deadline = episode.minimum_end_at
            if deadline is None:
                baseline = episode.last_activity_at or episode.last_event_time or episode.start_time
                deadline = baseline + timedelta(seconds=timeout)
            close_at = deadline + timedelta(seconds=grace)

            # A delayed lifecycle pass may first observe an active Episode only
            # after its complete settling horizon. Persist the state that is
            # true now instead of publishing an immediately stale intermediate
            # transition.
            if close_at < observed_at:
                await self._conn.execute(
                    "UPDATE episodes SET state = ?, end_time = ? WHERE id = ?",
                    (EpisodeState.CLOSED.value, _utc_iso(observed_at), episode.id),
                )
                episode.state = EpisodeState.CLOSED
                episode.end_time = observed_at
                closed.append(episode)
                continue

            if episode.state == EpisodeState.ACTIVE and deadline <= observed_at:
                await self._conn.execute(
                    "UPDATE episodes SET state = ? WHERE id = ?",
                    (EpisodeState.QUIESCENT.value, episode.id),
                )
                episode.state = EpisodeState.QUIESCENT
                quiescent.append(episode)

        if quiescent or closed:
            await self._conn.commit()
        for episode in quiescent:
            await asyncio.to_thread(
                append_journal,
                self._data_dir,
                episode.id,
                "episode.state_changed",
                {"state": EpisodeState.QUIESCENT.value},
            )
            await self.refresh_episode_manifest(episode.id)
        for episode in closed:
            await asyncio.to_thread(
                append_journal,
                self._data_dir,
                episode.id,
                "episode.state_changed",
                {"state": EpisodeState.CLOSED.value},
            )
            await self.refresh_episode_manifest(episode.id)
        return quiescent, closed

    async def append_episode_journal(
        self,
        episode_id: str,
        entry_type: str,
        data: dict | None = None,
    ) -> None:
        await asyncio.to_thread(
            append_journal,
            self._data_dir,
            episode_id,
            entry_type,
            data,
        )

    # --- Portable Episode bundles ---

    async def rebuild_episode_manifests(self) -> None:
        await self._bundles.rebuild()

    async def refresh_episode_manifest(self, episode_id: str) -> None:
        await self._bundles.refresh(episode_id)

    # --- Row deserialization ---

    @staticmethod
    def _row_to_evidence(row: aiosqlite.Row) -> Evidence:
        expired_at = row["retention_expired_at"] if "retention_expired_at" in row.keys() else None
        expiration_reason = (
            row["retention_expiration_reason"]
            if "retention_expiration_reason" in row.keys()
            else None
        )
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
            availability="expired" if expired_at else "available",
            expired_at=datetime.fromisoformat(expired_at) if expired_at else None,
            expiration_reason=expiration_reason,
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
            minimum_end_at=datetime.fromisoformat(row["minimum_end_at"])
            if row["minimum_end_at"]
            else None,
            end_time=datetime.fromisoformat(row["end_time"]) if row["end_time"] else None,
            state=EpisodeState(row["state"]),
            event_count=row["event_count"],
            evidence_count=row["evidence_count"],
            summary=row["summary"],
        )
