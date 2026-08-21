from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from episode.config import EpisodeConfig
from episode.connectors.event_api import EventAPIConnector
from episode.domain.models import Area, Device, ReceiptStatus
from episode.engine.bus import EventBus
from episode.engine.engine import EpisodeEngine
from episode.ingestion.router import IngressRouter
from episode.ingestion.service import IngestionService
from episode.storage.repository import Repository


async def _event_api(tmp_path):
    config = EpisodeConfig(data_dir=str(tmp_path / "data"), episode_timeout=30)
    repository = Repository(config)
    await repository.initialize()
    await repository.upsert_area(Area(id="entrance", name="Entrance"))
    await repository.upsert_device(
        Device(
            id="door-sensor",
            name="Door sensor",
            device_type="sensor",
            area_id="entrance",
        )
    )
    bus = EventBus()
    engine = EpisodeEngine(repository, bus, timeout=30)
    await engine.start()
    router = IngressRouter()
    ingestion = IngestionService(config.data_dir, repository, engine, router)
    connector = EventAPIConnector("Event API", ingestion, router, {}, 8989)
    app = FastAPI()
    connector.mount(app)
    await connector.start()
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("192.0.2.40", 1234)),
        base_url="http://test",
    )
    return repository, engine, connector, client


@pytest.mark.asyncio
async def test_event_api_preserves_request_then_creates_an_episode(tmp_path):
    repository, engine, connector, client = await _event_api(tmp_path)
    timestamp = datetime.now(tz=timezone.utc).isoformat()
    raw = (
        '{"device_id":"door-sensor","event_type":"doorbell",'
        f'"timestamp":"{timestamp}","source":"home-assistant",'
        '"external_id":"automation-42","metadata":{"friendly_name":"Front bell"}}'
    ).encode()
    try:
        response = await client.post(
            "/api/v1/events",
            content=raw,
            headers={"content-type": "application/json"},
        )
        schema = (await client.get("/openapi.json")).json()

        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "accepted"
        assert body["duplicate"] is False
        assert body["event_id"]
        assert body["episode_id"]
        request_schema = schema["paths"]["/api/v1/events"]["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"]
        assert set(request_schema["required"]) == {"device_id", "event_type"}
        response_schema = schema["paths"]["/api/v1/events"]["post"]["responses"]["201"]["content"][
            "application/json"
        ]["schema"]
        assert response_schema["$ref"].endswith("/EventSubmissionResponse")

        event = await repository.get_event(body["event_id"])
        receipt = await repository.get_ingestion_receipt(body["receipt_id"])
        artifact = await repository.get_raw_artifact(receipt.artifact_id)
        assert event.device_id == "door-sensor"
        assert event.area_id == "entrance"
        assert event.event_type == "doorbell"
        assert event.source == "event-api:home-assistant"
        assert event.metadata["friendly_name"] == "Front bell"
        assert receipt.external_id == "automation-42"
        assert receipt.event_id == event.id
        assert receipt.episode_id == event.episode_id
        assert receipt.metadata["client_ip"] == "192.0.2.40"
        assert Path(artifact.file_path).read_bytes() == raw
        assert artifact.sealed
        assert connector.status()["events_accepted"] == 1
    finally:
        await client.aclose()
        await connector.stop()
        await engine.stop()
        await repository.close()


@pytest.mark.asyncio
async def test_event_api_idempotency_key_links_retries_to_one_event(tmp_path):
    repository, engine, connector, client = await _event_api(tmp_path)
    try:
        first = await client.post(
            "/api/v1/events",
            json={
                "device_id": "door-sensor",
                "event_type": "manual_trigger",
                "source": "automation",
            },
            headers={"idempotency-key": "request-123"},
        )
        second = await client.post(
            "/api/v1/events",
            json={
                "device_id": "door-sensor",
                "event_type": "manual_trigger",
                "source": "automation",
            },
            headers={"idempotency-key": "request-123"},
        )

        assert first.status_code == 201
        assert second.status_code == 200
        assert second.json()["duplicate"] is True
        assert second.json()["event_id"] == first.json()["event_id"]
        assert second.json()["episode_id"] == first.json()["episode_id"]
        assert len(await repository.list_events()) == 1
        assert len(await repository.list_episodes()) == 1
        receipts = await repository.list_ingestion_receipts()
        assert len(receipts) == 2
        assert {receipt.external_id for receipt in receipts} == {"request-123"}
        assert {receipt.event_id for receipt in receipts} == {first.json()["event_id"]}
        assert connector.status()["duplicates"] == 1
    finally:
        await client.aclose()
        await connector.stop()
        await engine.stop()
        await repository.close()


@pytest.mark.asyncio
async def test_event_api_rejects_reused_idempotency_key_for_a_different_event(tmp_path):
    repository, engine, connector, client = await _event_api(tmp_path)
    try:
        first = await client.post(
            "/api/v1/events",
            json={
                "device_id": "door-sensor",
                "event_type": "doorbell",
                "source": "automation",
                "external_id": "request-123",
            },
        )
        conflict = await client.post(
            "/api/v1/events",
            json={
                "device_id": "door-sensor",
                "event_type": "door_unlock",
                "source": "automation",
                "external_id": "request-123",
            },
        )

        assert first.status_code == 201
        assert conflict.status_code == 422
        assert conflict.json()["status"] == "rejected"
        assert conflict.json()["reason"] == "event_identity_conflict"
        assert conflict.json()["event_id"] is None
        assert len(await repository.list_events()) == 1
        receipt = await repository.get_ingestion_receipt(conflict.json()["receipt_id"])
        assert receipt.status == ReceiptStatus.REJECTED
        assert receipt.event_id is None
        assert receipt.external_id == "request-123"
    finally:
        await client.aclose()
        await connector.stop()
        await engine.stop()
        await repository.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "content_type", "reason"),
    [
        (b'{"device_id":', "application/json", "invalid_json"),
        (b"plain text", "text/plain", "unsupported_media_type"),
        (
            b'{"device_id":"door-sensor","device_id":"other","event_type":"motion"}',
            "application/json",
            "invalid_json",
        ),
        (
            b'{"device_id":"door-sensor","event_type":"motion","timestamp":1}',
            "application/json",
            "invalid_event",
        ),
    ],
)
async def test_event_api_preserves_rejected_deliveries(
    tmp_path,
    payload,
    content_type,
    reason,
):
    repository, engine, connector, client = await _event_api(tmp_path)
    try:
        response = await client.post(
            "/api/v1/events",
            content=payload,
            headers={"content-type": content_type},
        )

        assert response.status_code == 422
        assert response.json()["status"] == "rejected"
        receipt = await repository.get_ingestion_receipt(response.json()["receipt_id"])
        artifact = await repository.get_raw_artifact(receipt.artifact_id)
        assert receipt.status == ReceiptStatus.REJECTED
        assert receipt.metadata["reason"] == reason
        assert Path(artifact.file_path).read_bytes() == payload
        assert await repository.list_events() == []
        assert connector.status()["requests_rejected"] == 1
    finally:
        await client.aclose()
        await connector.stop()
        await engine.stop()
        await repository.close()


@pytest.mark.asyncio
async def test_event_api_preserves_unknown_device_as_unmatched(tmp_path):
    repository, engine, connector, client = await _event_api(tmp_path)
    try:
        response = await client.post(
            "/api/v1/events",
            json={"device_id": "missing-sensor", "event_type": "tripwire"},
        )

        assert response.status_code == 422
        body = response.json()
        assert body["status"] == "unmatched"
        assert body["reason"] == "device_not_resolved"
        receipt = await repository.get_ingestion_receipt(body["receipt_id"])
        artifact = await repository.get_raw_artifact(receipt.artifact_id)
        assert receipt.status == ReceiptStatus.UNMATCHED
        assert receipt.device_id == "missing-sensor"
        assert Path(artifact.file_path).read_bytes()
        assert await repository.list_events() == []
        assert connector.status()["unmatched"] == 1
    finally:
        await client.aclose()
        await connector.stop()
        await engine.stop()
        await repository.close()
