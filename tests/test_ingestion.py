from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from episode.config import EpisodeConfig
from episode.connectors.http_ingress import HTTPIngressConnector
from episode.domain.models import Area, Device, ReceiptStatus
from episode.engine.bus import EventBus
from episode.engine.engine import EpisodeEngine
from episode.ingestion.models import (
    EventObservation,
    FileIngressDelivery,
    IngressDelivery,
    IngressHandlerResult,
    StoredIngressEnvelope,
)
from episode.ingestion.router import IngressHandlerRegistration, IngressRouter
from episode.ingestion.service import IngestionService
from episode.plugins.hikvision_alarm.plugin import HikvisionAlarmPlugin
from episode.plugins.models import PluginContext
from episode.storage.repository import Repository


async def _pipeline(tmp_path):
    config = EpisodeConfig(data_dir=str(tmp_path / "data"), episode_timeout=30)
    repository = Repository(config)
    await repository.initialize()
    await repository.upsert_area(Area(id="gate", name="Front gate"))
    await repository.upsert_device(
        Device(
            id="gate-camera",
            name="Gate camera",
            device_type="camera",
            area_id="gate",
            ip_address="192.0.2.10",
        )
    )
    bus = EventBus()
    engine = EpisodeEngine(repository, bus, timeout=30)
    await engine.start()
    router = IngressRouter()
    ingestion = IngestionService(config.data_dir, repository, engine, router)
    return config, repository, engine, router, ingestion


@pytest.mark.asyncio
async def test_delivery_is_durable_before_handler_runs(tmp_path):
    _config, repository, engine, router, ingestion = await _pipeline(tmp_path)
    observed: list[str] = []

    async def verify_durable(envelope: StoredIngressEnvelope) -> IngressHandlerResult:
        receipt = await repository.get_ingestion_receipt(envelope.receipt_id)
        artifact = await repository.get_raw_artifact(envelope.artifact_id)
        assert receipt is not None
        assert artifact is not None
        assert artifact.sealed
        assert Path(artifact.file_path).read_bytes() == b"unaltered payload"
        observed.append(receipt.id)
        return IngressHandlerResult(claimed=True)

    router.register(
        IngressHandlerRegistration(
            id="durability-check",
            matcher=lambda _envelope: True,
            handler=verify_durable,
        )
    )
    try:
        outcome = await ingestion.accept(
            IngressDelivery(
                source="test:raw",
                transport="test",
                received_at=datetime.now(tz=timezone.utc),
                payload=b"unaltered payload",
            )
        )
        assert observed == [outcome.receipt.id]
    finally:
        await engine.stop()
        await repository.close()


@pytest.mark.asyncio
async def test_delivery_artifact_and_receipt_roll_back_together(tmp_path, monkeypatch):
    _config, repository, engine, _router, ingestion = await _pipeline(tmp_path)

    async def fail_receipt(*_args, **_kwargs):
        raise RuntimeError("simulated receipt failure")

    monkeypatch.setattr(repository._delivery_provenance, "create_receipt", fail_receipt)
    try:
        with pytest.raises(RuntimeError, match="simulated receipt failure"):
            await ingestion.accept(
                IngressDelivery(
                    source="test:rollback",
                    transport="test",
                    received_at=datetime.now(tz=timezone.utc),
                    payload=b"preserved even when indexing fails",
                )
            )

        rows = await repository._conn.execute_fetchall("SELECT id FROM raw_artifacts")
        assert rows == []
        assert await repository.list_ingestion_receipts() == []
    finally:
        await engine.stop()
        await repository.close()


@pytest.mark.asyncio
async def test_file_is_preserved_when_plugin_handler_fails(tmp_path):
    _config, repository, engine, router, ingestion = await _pipeline(tmp_path)

    async def fail(_envelope):
        raise RuntimeError("broken file interpreter")

    router.register(IngressHandlerRegistration("broken-file", fail, lambda _item: True))
    upload = tmp_path / "unrecognized-camera-file.jpg"
    upload.write_bytes(b"raw file survives plugin failure")
    try:
        outcome = await ingestion.accept_file(
            FileIngressDelivery(
                source="ftp:upload",
                transport="ftp",
                received_at=datetime.now(tz=timezone.utc),
                file_path=upload,
                media_type="image/jpeg",
                original_filename=upload.name,
                metadata={"connector_type": "ftp"},
            )
        )

        artifact = await repository.get_raw_artifact(outcome.receipt.artifact_id)
        assert outcome.receipt.status == ReceiptStatus.REJECTED
        assert outcome.receipt.metadata["reason"] == "ingress_handler_failed"
        assert artifact.sealed
        assert Path(artifact.file_path).read_bytes() == b"raw file survives plugin failure"
        assert not upload.exists()
    finally:
        await engine.stop()
        await repository.close()


@pytest.mark.asyncio
async def test_router_isolates_failure_and_timeout():
    router = IngressRouter()

    async def fail(_envelope):
        raise RuntimeError("plugin bug")

    async def hang(_envelope):
        await asyncio.sleep(1)
        return IngressHandlerResult(claimed=True)

    router.register(IngressHandlerRegistration("failure", fail, lambda _item: True))
    router.register(IngressHandlerRegistration("timeout", hang, lambda _item: True, timeout=0.01))
    envelope = StoredIngressEnvelope(
        receipt_id="receipt",
        artifact_id="artifact",
        source="test",
        transport="test",
        received_at=datetime.now(tz=timezone.utc),
        payload=b"payload",
        media_type="application/octet-stream",
    )

    results = await router.dispatch(envelope)

    assert [(result.handler_id, result.state) for result in results] == [
        ("failure", "failed"),
        ("timeout", "timed_out"),
    ]
    assert router.status("failure")["failures"] == 1
    assert router.status("timeout")["timeouts"] == 1


@pytest.mark.asyncio
async def test_multiple_handler_claims_are_rejected_without_ordering_side_effects(tmp_path):
    _config, repository, engine, router, ingestion = await _pipeline(tmp_path)
    timestamp = datetime.now(tz=timezone.utc)

    async def claim(_envelope):
        return IngressHandlerResult(
            claimed=True,
            event=EventObservation(
                timestamp=timestamp,
                event_type="motion_detection",
                event_state="active",
                source="test:plugin",
                device_id="gate-camera",
                area_id="gate",
            ),
        )

    router.register(IngressHandlerRegistration("first", claim, lambda _item: True))
    router.register(IngressHandlerRegistration("second", claim, lambda _item: True))
    try:
        outcome = await ingestion.accept(
            IngressDelivery(
                source="test:conflict",
                transport="test",
                received_at=timestamp,
                payload=b"ambiguous payload",
            )
        )

        assert outcome.receipt.status == ReceiptStatus.REJECTED
        assert outcome.receipt.metadata["reason"] == ("multiple_ingress_handlers_claimed_delivery")
        assert await repository.list_events() == []
        artifact = await repository.get_raw_artifact(outcome.receipt.artifact_id)
        assert Path(artifact.file_path).read_bytes() == b"ambiguous payload"
    finally:
        await engine.stop()
        await repository.close()


@pytest.mark.asyncio
async def test_alarm_server_preserves_full_request_then_plugin_creates_event(tmp_path):
    config, repository, engine, router, ingestion = await _pipeline(tmp_path)
    plugin = HikvisionAlarmPlugin(PluginContext(Path(config.plugins_dir), ingress_router=router))
    await plugin.start()
    connector = HTTPIngressConnector(
        "Alarm Server",
        ingestion,
        {"path": "/alarm"},
        8080,
        connector_type="alarm_server",
    )
    app = FastAPI()
    connector.mount(app)
    await connector.start()
    xml = (Path(__file__).parent / "fixtures/hikvision/alarm_event.xml").read_bytes()
    payload = (
        b"--episode-boundary\r\nContent-Type: application/xml\r\n\r\n"
        + xml
        + b"\r\n--episode-boundary--\r\n"
    )

    try:
        transport = httpx.ASGITransport(app=app, client=("192.0.2.10", 4567))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/alarm",
                content=payload,
                headers={"content-type": "multipart/form-data; boundary=episode-boundary"},
            )

        assert response.status_code == 200
        events = await repository.list_events()
        episodes = await repository.list_episodes()
        receipts = await repository.list_ingestion_receipts()
        assert len(events) == len(episodes) == len(receipts) == 1
        assert events[0].event_type == "human_detection"
        assert events[0].device_id == "gate-camera"
        assert events[0].metadata["target_type"] == "human"
        assert events[0].metadata["bounding_box"] == {
            "x": 0.125,
            "y": 0.25,
            "width": 0.5,
            "height": 0.625,
        }
        assert receipts[0].status == ReceiptStatus.ACCEPTED
        assert receipts[0].event_id == events[0].id
        artifact = await repository.get_raw_artifact(receipts[0].artifact_id)
        assert artifact is not None
        assert Path(artifact.file_path).read_bytes() == payload
        assert artifact.sealed
        assert events[0].raw_payload_path == artifact.file_path
        assert plugin.status().metrics["deliveries"] == 1
        assert plugin.status().metrics["claimed"] == 1
    finally:
        await connector.stop()
        await plugin.stop()
        await engine.stop()
        await repository.close()


@pytest.mark.asyncio
async def test_alarm_server_keeps_unclaimed_payload_without_an_event(tmp_path):
    config, repository, engine, router, ingestion = await _pipeline(tmp_path)
    plugin = HikvisionAlarmPlugin(PluginContext(Path(config.plugins_dir), ingress_router=router))
    await plugin.start()
    connector = HTTPIngressConnector(
        "Alarm Server",
        ingestion,
        {},
        8080,
        connector_type="alarm_server",
    )
    app = FastAPI()
    connector.mount(app)

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/alarm", content=b"unknown but preserved")

        assert response.status_code == 200
        assert await repository.list_events() == []
        receipts = await repository.list_ingestion_receipts()
        assert len(receipts) == 1
        assert receipts[0].status == ReceiptStatus.ACCEPTED
        assert receipts[0].metadata["ingress_handlers"] == []
        artifact = await repository.get_raw_artifact(receipts[0].artifact_id)
        assert Path(artifact.file_path).read_bytes() == b"unknown but preserved"
    finally:
        await plugin.stop()
        await engine.stop()
        await repository.close()
