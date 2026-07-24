from __future__ import annotations

import sqlite3

import pytest

from episode.config import EpisodeConfig
from episode.domain.models import (
    Area,
    Device,
    Episode,
    Event,
    Evidence,
    IngestionReceipt,
)
from episode.storage.repository import Repository


@pytest.mark.asyncio
async def test_migrated_legacy_database_accepts_new_identity_writes(tmp_path):
    database = tmp_path / "episode.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE assets (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, location TEXT NOT NULL DEFAULT '',
            metadata TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE sensors (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, sensor_type TEXT NOT NULL,
            asset_id TEXT NOT NULL, capabilities TEXT NOT NULL DEFAULT '[]',
            ip_address TEXT NOT NULL DEFAULT '', username TEXT NOT NULL DEFAULT '',
            password TEXT NOT NULL DEFAULT '', configs TEXT NOT NULL DEFAULT '{}',
            metadata TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE episodes (
            id TEXT PRIMARY KEY, primary_asset_id TEXT NOT NULL, start_time TEXT NOT NULL,
            last_event_time TEXT, end_time TEXT, state TEXT NOT NULL DEFAULT 'new',
            event_count INTEGER NOT NULL DEFAULT 0,
            evidence_count INTEGER NOT NULL DEFAULT 0, summary TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE events (
            id TEXT PRIMARY KEY, sensor_id TEXT NOT NULL, asset_id TEXT NOT NULL,
            timestamp TEXT NOT NULL, event_type TEXT NOT NULL,
            event_state TEXT NOT NULL DEFAULT 'active', source TEXT NOT NULL DEFAULT '',
            raw_payload_path TEXT, metadata TEXT NOT NULL DEFAULT '{}', episode_id TEXT
        );
        CREATE TABLE evidence (
            id TEXT PRIMARY KEY, sensor_id TEXT NOT NULL, asset_id TEXT NOT NULL,
            timestamp TEXT NOT NULL, evidence_type TEXT NOT NULL, file_path TEXT NOT NULL,
            mime_type TEXT NOT NULL DEFAULT '', original_filename TEXT,
            metadata TEXT NOT NULL DEFAULT '{}', event_id TEXT, episode_id TEXT
        );
        CREATE TABLE ingestion_receipts (
            id TEXT PRIMARY KEY, source TEXT NOT NULL, received_at TEXT NOT NULL,
            observed_at TEXT, status TEXT NOT NULL, artifact_id TEXT,
            sensor_id TEXT NOT NULL DEFAULT '', asset_id TEXT NOT NULL DEFAULT '',
            external_id TEXT, metadata TEXT NOT NULL DEFAULT '{}', event_id TEXT,
            evidence_id TEXT, episode_id TEXT
        );
        """
    )
    connection.close()

    repo = Repository(EpisodeConfig(data_dir=str(tmp_path), db_path=str(database)))
    await repo.initialize()
    try:
        await repo.upsert_area(Area(id="gate", name="Front gate"))
        await repo.upsert_device(
            Device(
                id="camera-gate",
                name="Gate camera",
                device_type="camera",
                area_id="gate",
                ip_address="192.0.2.10",
            )
        )
        episode = await repo.create_episode(Episode(primary_area_id="gate"))
        event = await repo.create_event(
            Event(
                device_id="camera-gate",
                area_id="gate",
                event_type="motion",
                episode_id=episode.id,
            )
        )
        evidence = await repo.create_evidence(
            Evidence(
                device_id="camera-gate",
                area_id="gate",
                evidence_type="snapshot",
                file_path=str(tmp_path / "snapshot.jpg"),
                event_id=event.id,
                episode_id=episode.id,
            )
        )
        await repo.create_ingestion_receipt(
            IngestionReceipt(
                source="onvif",
                device_id="camera-gate",
                area_id="gate",
                event_id=event.id,
                evidence_id=evidence.id,
                episode_id=episode.id,
            )
        )

        event_row = (
            await repo._conn.execute_fetchall(
                "SELECT sensor_id, asset_id FROM events WHERE id = ?", (event.id,)
            )
        )[0]
        evidence_row = (
            await repo._conn.execute_fetchall(
                "SELECT sensor_id, asset_id FROM evidence WHERE id = ?", (evidence.id,)
            )
        )[0]
        episode_row = (
            await repo._conn.execute_fetchall(
                "SELECT primary_asset_id FROM episodes WHERE id = ?", (episode.id,)
            )
        )[0]
        receipt_row = (
            await repo._conn.execute_fetchall(
                "SELECT sensor_id, asset_id FROM ingestion_receipts WHERE event_id = ?",
                (event.id,),
            )
        )[0]

        assert tuple(event_row) == ("camera-gate", "gate")
        assert tuple(evidence_row) == ("camera-gate", "gate")
        assert episode_row["primary_asset_id"] == "gate"
        assert tuple(receipt_row) == ("camera-gate", "gate")
    finally:
        await repo.close()
