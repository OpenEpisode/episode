from datetime import datetime, timedelta, timezone

import pytest

from episode.config import EpisodeConfig
from episode.connectors.hikvision.ftp import FTPConnector
from episode.domain.models import Area, Device, Evidence
from episode.engine.bus import EventBus
from episode.media.timelapse import is_timelapse_eligible
from episode.storage.repository import Repository


def test_parses_video_intercom_ftp_filename():
    parsed = FTPConnector._parse_filename("20260805154705_4_192.168.40.20.jpg")

    assert parsed == {
        "ip_address": "192.168.40.20",
        "timestamp": datetime(2026, 8, 5, 15, 47, 5, tzinfo=timezone(timedelta(hours=1))),
        "event_type": "4",
        "filename_profile": "video_intercom",
    }


@pytest.mark.asyncio
async def test_doorbell_ftp_snapshot_is_an_event_attachment(tmp_path):
    config = EpisodeConfig(data_dir=str(tmp_path / "data"))
    repository = Repository(config)
    await repository.initialize()
    try:
        await repository.upsert_area(Area(id="front", name="Front"))
        await repository.upsert_device(
            Device(
                id="front-doorbell",
                name="Front Doorbell",
                device_type="hikvision",
                area_id="front",
                capabilities=["doorbell", "video", "hikvision_sdk"],
                ip_address="192.168.40.20",
            )
        )

        deliveries = []

        async def capture(message):
            deliveries.append(message)

        bus = EventBus()
        bus.subscribe("evidence.received", capture)
        connector = FTPConnector("FTP", bus, {}, config, repository)

        upload = tmp_path / "20260805154705_4_192.168.40.20.jpg"
        upload.write_bytes(b"immutable jpeg bytes")
        await connector._ingest_snapshot(str(upload))

        assert len(deliveries) == 1
        evidence = deliveries[0].data["evidence"]
        assert evidence["device_id"] == "front-doorbell"
        assert evidence["area_id"] == "front"
        assert evidence["metadata"] == {
            "ip_address": "192.168.40.20",
            "event_type": "4",
            "filename_profile": "video_intercom",
            "evidence_role": "event_attachment",
            "timelapse_eligible": False,
            "origin": "ftp",
        }
    finally:
        await repository.close()


def test_timelapse_eligibility_is_explicit_metadata():
    ordinary = Evidence(evidence_type="snapshot")
    attachment = Evidence(
        evidence_type="snapshot",
        metadata={"timelapse_eligible": False},
    )

    assert is_timelapse_eligible(ordinary)
    assert not is_timelapse_eligible(attachment)
