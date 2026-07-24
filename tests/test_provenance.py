from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone

import httpx
import pytest

from episode.api.routes import create_api
from episode.config import EpisodeConfig
from episode.connectors.hikvision.parser import ingest_hikvision_xml
from episode.domain.models import Area, Device, EventState, IngestionReceipt, ReceiptStatus
from episode.engine.bus import EventBus, Message
from episode.engine.engine import EpisodeEngine
from episode.storage.files import describe_artifact
from episode.storage.repository import Repository


async def _configured_repo(tmp_path) -> tuple[EpisodeConfig, Repository]:
    config = EpisodeConfig(
        data_dir=str(tmp_path),
        db_path=str(tmp_path / "episode.db"),
        episode_timeout=30,
    )
    repo = Repository(config)
    await repo.initialize()
    await repo.upsert_area(Area(id="gate", name="Front gate"))
    await repo.upsert_device(
        Device(
            id="camera-gate",
            name="Gate camera",
            device_type="hikvision",
            area_id="gate",
            ip_address="192.0.2.10",
        )
    )
    return config, repo


@pytest.mark.asyncio
async def test_duplicate_deliveries_share_canonical_event_and_bundle(tmp_path):
    config, repo = await _configured_repo(tmp_path)
    bus = EventBus()
    engine = EpisodeEngine(repo, bus, timeout=30)
    await engine.start()
    try:
        observed_at = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
        event_template = {
            "device_id": "camera-gate",
            "area_id": "gate",
            "timestamp": observed_at,
            "event_type": "human_detection",
            "event_state": EventState.ACTIVE.value,
        }

        for source, filename, payload in (
            ("hikvision:isapi", "isapi.xml", b"<isapi>original one</isapi>"),
            (
                "hikvision:alarm_server",
                "alarm.xml",
                b"<alarm>original two</alarm>",
            ),
        ):
            path = tmp_path / "orphans" / "events" / filename
            path.write_bytes(payload)
            artifact = describe_artifact(str(path), "event_payload", "application/xml")
            receipt = IngestionReceipt(
                source=source,
                observed_at=observed_at,
                artifact_id=artifact.id,
                device_id="camera-gate",
                area_id="gate",
            )
            await bus.publish(
                Message(
                    type="event.received",
                    data={
                        "event": {
                            **event_template,
                            "source": source,
                            "raw_payload_path": str(path),
                        },
                        "artifact": asdict(artifact),
                        "receipt": asdict(receipt),
                    },
                )
            )

        events = await repo.list_events()
        episodes = await repo.list_episodes()
        receipts = await repo.list_ingestion_receipts(episode_id=episodes[0].id)

        assert len(events) == 1
        assert episodes[0].event_count == 1
        assert len(receipts) == 2
        assert {receipt.source for receipt in receipts} == {
            "hikvision:isapi",
            "hikvision:alarm_server",
        }
        assert {receipt.event_id for receipt in receipts} == {events[0].id}

        transport = httpx.ASGITransport(app=create_api(repo, str(tmp_path)))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            event_body = (await client.get(f"/api/v1/events/{events[0].id}")).json()
            receipt_body = (await client.get(f"/api/v1/receipts?event_id={events[0].id}")).json()
            artifact_response = await client.get(f"/api/v1/receipts/{receipts[0].id}/artifact")

        assert set(event_body["sources"]) == {
            "hikvision:isapi",
            "hikvision:alarm_server",
        }
        assert len(receipt_body) == 2
        assert all("file_path" not in item for item in receipt_body)
        assert artifact_response.content in {
            b"<isapi>original one</isapi>",
            b"<alarm>original two</alarm>",
        }

        manifest_path = tmp_path / "episodes" / episodes[0].id / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        assert manifest["format"] == "episode.bundle"
        assert len(manifest["events"]) == 1
        assert len(manifest["receipts"]) == 2
        assert len(manifest["artifacts"]) == 2
        assert {artifact["sha256"] for artifact in manifest["artifacts"]} == {
            receipt_artifact.sha256
            for receipt_artifact in [
                await repo.get_raw_artifact(receipt.artifact_id) for receipt in receipts
            ]
        }
        for artifact in manifest["artifacts"]:
            stored = manifest_path.parent / artifact["file"]
            assert stored.exists()
            assert stored.stat().st_mode & 0o222 == 0
    finally:
        await engine.stop()
        await repo.close()


@pytest.mark.asyncio
async def test_rejected_delivery_is_preserved_without_creating_event(tmp_path):
    config, repo = await _configured_repo(tmp_path)
    bus = EventBus()
    engine = EpisodeEngine(repo, bus, timeout=30)
    await engine.start()
    try:
        delivery = ingest_hikvision_xml(
            b"not valid XML",
            "camera-gate",
            "gate",
            "hikvision:isapi",
            config.orphans_dir,
        )
        assert delivery.event is None
        await bus.publish(
            Message(
                type="receipt.received",
                data={
                    "artifact": asdict(delivery.artifact),
                    "receipt": asdict(delivery.receipt),
                },
            )
        )

        receipts = await repo.list_ingestion_receipts()
        assert len(receipts) == 1
        assert receipts[0].status == ReceiptStatus.REJECTED
        assert await repo.list_events() == []
        artifact = await repo.get_raw_artifact(receipts[0].artifact_id)
        assert artifact is not None
        assert artifact.sha256 == delivery.artifact.sha256
        assert artifact.byte_size == len(b"not valid XML")
    finally:
        await engine.stop()
        await repo.close()


@pytest.mark.asyncio
async def test_legacy_database_receives_additive_provenance_columns(tmp_path):
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
        """
    )
    connection.close()

    config = EpisodeConfig(data_dir=str(tmp_path), db_path=str(database))
    repo = Repository(config)
    await repo.initialize()
    try:
        event_columns = {
            row["name"] for row in await repo._conn.execute_fetchall("PRAGMA table_info(events)")
        }
        evidence_columns = {
            row["name"] for row in await repo._conn.execute_fetchall("PRAGMA table_info(evidence)")
        }
        tables = {
            row["name"]
            for row in await repo._conn.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"device_id", "area_id", "dedup_key"} <= event_columns
        assert {"device_id", "area_id", "artifact_id", "byte_size", "sha256"} <= (evidence_columns)
        assert {"areas", "devices", "raw_artifacts", "ingestion_receipts"} <= tables
    finally:
        await repo.close()
