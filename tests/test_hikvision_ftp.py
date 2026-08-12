from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from episode.config import EpisodeConfig
from episode.connectors.ftp import FTPConnector
from episode.domain.models import Area, Device, Evidence, ReceiptStatus
from episode.engine.bus import EventBus
from episode.engine.engine import EpisodeEngine
from episode.ingestion.router import IngressRouter
from episode.ingestion.service import IngestionService
from episode.media.timelapse import is_timelapse_eligible
from episode.plugins.hikvision_ftp.plugin import HikvisionFTPPlugin, parse_hikvision_filename
from episode.plugins.models import PluginContext
from episode.storage.repository import Repository


def test_parses_video_intercom_ftp_filename():
    parsed = parse_hikvision_filename("20260805154705_4_192.168.40.20.jpg")

    assert parsed == {
        "ip_address": "192.168.40.20",
        "timestamp": datetime(2026, 8, 5, 15, 47, 5, tzinfo=timezone(timedelta(hours=1))),
        "event_type": "4",
        "filename_profile": "video_intercom",
    }


async def _ftp_pipeline(tmp_path, *, doorbell: bool = False):
    config = EpisodeConfig(data_dir=str(tmp_path / "data"))
    repository = Repository(config)
    await repository.initialize()
    await repository.upsert_area(Area(id="front", name="Front"))
    capabilities = ["doorbell", "video", "hikvision_sdk"] if doorbell else ["video"]
    device_type = "doorbell" if doorbell else "camera"
    device = Device(
        id="front-device",
        name="Front device",
        device_type=device_type,
        area_id="front",
        capabilities=capabilities,
        ip_address="192.168.40.20",
    )
    await repository.upsert_device(device)
    bus = EventBus()
    engine = EpisodeEngine(repository, bus, timeout=30)
    await engine.start()
    router = IngressRouter()
    ingestion = IngestionService(config.data_dir, repository, engine, router)
    configured = (
        {
            "id": device.id,
            "device_type": device.device_type,
            "capabilities": device.capabilities,
            "ip_address": device.ip_address,
        },
    )
    plugin = HikvisionFTPPlugin(
        PluginContext(
            Path(config.plugins_dir), configured_devices=configured, ingress_router=router
        )
    )
    await plugin.start()
    connector = FTPConnector("FTP", ingestion, {}, config)
    return repository, engine, plugin, connector


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "expected_event_type"),
    [
        ("20260805154705_4_192.168.40.20.jpg", "4"),
        ("192.168.40.20_01_20260805154705123_human.jpg", "human"),
    ],
)
async def test_hikvision_ftp_plugin_creates_snapshot_evidence(
    tmp_path, filename, expected_event_type
):
    repository, engine, plugin, connector = await _ftp_pipeline(tmp_path)
    try:
        upload = tmp_path / filename
        upload.write_bytes(b"immutable jpeg bytes")
        await connector._ingest_file(str(upload), "192.0.2.40")

        evidence = (await repository.list_evidence())[0]
        receipts = await repository.list_ingestion_receipts()
        artifact = await repository.get_raw_artifact(receipts[0].artifact_id)
        assert evidence.device_id == "front-device"
        assert evidence.area_id == "front"
        assert evidence.metadata["event_type"] == expected_event_type
        assert evidence.metadata["origin"] == "ftp"
        assert evidence.metadata["ingress_handler"] == "hikvision-ftp-snapshots"
        assert evidence.metadata["interpretation_source"] == "hikvision:ftp"
        assert receipts[0].status == ReceiptStatus.ACCEPTED
        assert receipts[0].evidence_id == evidence.id
        assert receipts[0].metadata["vendor"] == "hikvision"
        assert artifact.sealed
        assert Path(artifact.file_path).read_bytes() == b"immutable jpeg bytes"
        assert plugin.status().metrics["claimed"] == 1
        assert connector.status()["uploads_received"] == 1
    finally:
        await plugin.stop()
        await engine.stop()
        await repository.close()


@pytest.mark.asyncio
async def test_doorbell_ftp_snapshot_is_an_event_attachment(tmp_path):
    repository, engine, plugin, connector = await _ftp_pipeline(tmp_path, doorbell=True)
    try:
        filename = "20260805154705_4_192.168.40.20.jpg"
        upload = tmp_path / filename
        upload.write_bytes(b"immutable jpeg bytes")
        await connector._ingest_file(str(upload))

        evidence = (await repository.list_evidence())[0]
        assert evidence.metadata["evidence_role"] == "event_attachment"
        assert evidence.metadata["timelapse_eligible"] is False
        assert not is_timelapse_eligible(evidence)
    finally:
        await plugin.stop()
        await engine.stop()
        await repository.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["unknown.jpg", "camera-message.txt"])
async def test_unknown_ftp_file_is_preserved_without_creating_evidence(tmp_path, filename):
    repository, engine, plugin, connector = await _ftp_pipeline(tmp_path)
    try:
        upload = tmp_path / filename
        upload.write_bytes(b"unknown but preserved")
        await connector._ingest_file(str(upload))

        assert await repository.list_evidence() == []
        receipts = await repository.list_ingestion_receipts()
        artifact = await repository.get_raw_artifact(receipts[0].artifact_id)
        assert receipts[0].status == ReceiptStatus.ACCEPTED
        assert receipts[0].metadata["ingress_handlers"] == []
        assert artifact.original_filename == filename
        assert Path(artifact.file_path).read_bytes() == b"unknown but preserved"
        assert plugin.status().metrics["deliveries"] == 0
    finally:
        await plugin.stop()
        await engine.stop()
        await repository.close()


@pytest.mark.asyncio
async def test_duplicate_ftp_filenames_never_overwrite_raw_evidence(tmp_path):
    repository, engine, plugin, connector = await _ftp_pipeline(tmp_path)
    try:
        filename = "20260805154705_4_192.168.40.20.jpg"
        upload = tmp_path / filename
        upload.write_bytes(b"first jpeg")
        await connector._ingest_file(str(upload))
        upload.write_bytes(b"second jpeg")
        await connector._ingest_file(str(upload))

        receipts = await repository.list_ingestion_receipts(limit=10)
        artifacts = [await repository.get_raw_artifact(receipt.artifact_id) for receipt in receipts]
        assert len(await repository.list_evidence()) == 2
        assert len({artifact.file_path for artifact in artifacts}) == 2
        assert {Path(artifact.file_path).read_bytes() for artifact in artifacts} == {
            b"first jpeg",
            b"second jpeg",
        }
        assert {artifact.original_filename for artifact in artifacts} == {filename}
    finally:
        await plugin.stop()
        await engine.stop()
        await repository.close()


def test_timelapse_eligibility_is_explicit_metadata():
    ordinary = Evidence(evidence_type="snapshot")
    attachment = Evidence(
        evidence_type="snapshot",
        metadata={"timelapse_eligible": False},
    )

    assert is_timelapse_eligible(ordinary)
    assert not is_timelapse_eligible(attachment)
