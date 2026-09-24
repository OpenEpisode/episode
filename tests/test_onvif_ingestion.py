from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from episode.config import EpisodeConfig
from episode.domain.models import Area, Device, ReceiptStatus
from episode.engine.bus import EventBus
from episode.engine.engine import EpisodeEngine
from episode.ingestion.router import IngressRouter
from episode.ingestion.service import IngestionService
from episode.media.registry import MediaRegistry
from episode.plugins.deliveries import RawPluginDeliveryStore
from episode.plugins.models import (
    PluginContext,
    PluginInstanceState,
    PluginInstanceStatus,
    RawPluginDelivery,
)
from episode.plugins.onvif.plugin import ONVIFPlugin
from episode.storage.repository import Repository

ACTIVE_NOTIFICATION = b"""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
 xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2"
 xmlns:tt="http://www.onvif.org/ver10/schema">
 <s:Body><PullMessagesResponse>
  <wsnt:NotificationMessage>
   <wsnt:Topic>tns1:RuleEngine/TamperDetector/Tamper</wsnt:Topic>
   <wsnt:Message><tt:Message UtcTime="2026-07-23T13:26:00Z"
     PropertyOperation="Changed"><tt:Data>
    <tt:SimpleItem Name="IsTamper" Value="true"/>
   </tt:Data></tt:Message></wsnt:Message>
  </wsnt:NotificationMessage>
 </PullMessagesResponse></s:Body>
</s:Envelope>"""


UNMAPPED_NOTIFICATION = ACTIVE_NOTIFICATION.replace(
    b"tns1:RuleEngine/TamperDetector/Tamper",
    b"tns1:VendorCustom/UnnamedDetector",
).replace(b'Name="IsTamper"', b'Name="State"')


class _FakeONVIFConnection:
    """A started Device connection, so plugin status has an instance to report on."""

    def __init__(self, config, _sink, _media=None, _update_sink=None):
        self.config = config
        self._status = PluginInstanceStatus(
            id=config.device.id,
            name=config.device.name,
            state=PluginInstanceState.RUNNING,
        )

    def status(self) -> PluginInstanceStatus:
        return self._status

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


@pytest.mark.asyncio
async def test_unmapped_topic_is_preserved_and_reported_without_becoming_an_event(tmp_path):
    """A topic this adapter cannot name stays preserved but never becomes a canonical
    Event, so it can never be filtered into silence. It is reported on the Device
    status instead of being guessed at."""
    config = EpisodeConfig(data_dir=str(tmp_path / "data"), episode_timeout=30)
    repository = Repository(config)
    await repository.initialize()
    await repository.upsert_area(Area(id="entrance", name="Entrance"))
    await repository.upsert_device(
        Device(id="camera-1", name="Camera", area_id="entrance", device_type="camera")
    )
    engine = EpisodeEngine(repository, EventBus(), timeout=30)
    await engine.start()
    router = IngressRouter()
    ingestion = IngestionService(config.data_dir, repository, engine, router)
    sink = RawPluginDeliveryStore(ingestion)
    plugin = ONVIFPlugin(
        PluginContext(
            tmp_path,
            configured_devices=(
                {
                    "id": "camera-1",
                    "name": "Camera",
                    "area_id": "entrance",
                    "ip_address": "192.0.2.10",
                    "username": "operator",
                    "password": "secret",
                    "configs": {"onvif": {}},
                },
            ),
            raw_delivery_sink=sink,
            ingress_router=router,
            media_registry=MediaRegistry(),
            device_update_sink=repository.upsert_device,
        ),
        connection_factory=_FakeONVIFConnection,
    )
    await plugin.start()
    try:
        for _delivery in range(2):
            await sink(
                RawPluginDelivery(
                    plugin_id="onvif",
                    device_id="camera-1",
                    area_id="entrance",
                    received_at=datetime.now(tz=timezone.utc),
                    payload=UNMAPPED_NOTIFICATION,
                    source="onvif:events",
                    media_type="application/soap+xml",
                    artifact_type="event_batch",
                    metadata={"kind": "pull_response"},
                )
            )

        assert await repository.list_events() == [], "no canonical Event from an unnamed topic"
        receipts = await repository.list_ingestion_receipts()
        unmapped = [
            receipt for receipt in receipts if receipt.metadata.get("reason") == "unmapped_topic"
        ]
        assert len(unmapped) == 2
        assert all(receipt.status == ReceiptStatus.IGNORED for receipt in unmapped)
        # Both levels stay sealed and retrievable, so the operator can name the topic.
        for receipt in unmapped:
            artifact = await repository.get_raw_artifact(receipt.artifact_id)
            assert b"tns1:VendorCustom/UnnamedDetector" in Path(artifact.file_path).read_bytes()
        for receipt in receipts:
            if receipt.metadata.get("reason") != "expanded_to_notifications":
                continue
            artifact = await repository.get_raw_artifact(receipt.artifact_id)
            assert Path(artifact.file_path).read_bytes() == UNMAPPED_NOTIFICATION

        instance = plugin.status().instances[0]
        assert instance.details["unmapped_topics"] == {"tns1:VendorCustom/UnnamedDetector": 2}
        assert instance.details["events_received"] == 0
    finally:
        await plugin.stop()
        await engine.stop()
        await repository.close()


@pytest.mark.asyncio
async def test_repeated_state_keeps_source_response_and_derived_receipts(tmp_path):
    config = EpisodeConfig(data_dir=str(tmp_path / "data"), episode_timeout=30)
    repository = Repository(config)
    await repository.initialize()
    await repository.upsert_area(Area(id="entrance", name="Entrance"))
    await repository.upsert_device(
        Device(id="camera-1", name="Camera", area_id="entrance", device_type="camera")
    )
    engine = EpisodeEngine(repository, EventBus(), timeout=30)
    await engine.start()
    router = IngressRouter()
    ingestion = IngestionService(config.data_dir, repository, engine, router)
    sink = RawPluginDeliveryStore(ingestion)
    plugin = ONVIFPlugin(
        PluginContext(
            tmp_path,
            raw_delivery_sink=sink,
            ingress_router=router,
            media_registry=MediaRegistry(),
            device_update_sink=repository.upsert_device,
        )
    )
    await plugin.start()
    try:
        for _delivery in range(2):
            await sink(
                RawPluginDelivery(
                    plugin_id="onvif",
                    device_id="camera-1",
                    area_id="entrance",
                    received_at=datetime.now(tz=timezone.utc),
                    payload=ACTIVE_NOTIFICATION,
                    source="onvif:events",
                    media_type="application/soap+xml",
                    artifact_type="event_batch",
                    metadata={"kind": "pull_response"},
                )
            )

        events = await repository.list_events()
        receipts = await repository.list_ingestion_receipts()
        assert len(events) == 1
        assert events[0].event_type == "tamper_detection"
        assert len(receipts) == 4
        assert sum(receipt.status == ReceiptStatus.ACCEPTED for receipt in receipts) == 1
        assert sum(receipt.status == ReceiptStatus.IGNORED for receipt in receipts) == 3
        assert any(receipt.metadata.get("reason") == "repeated_state" for receipt in receipts)
        source_receipts = [
            receipt
            for receipt in receipts
            if receipt.metadata.get("reason") == "expanded_to_notifications"
        ]
        assert len(source_receipts) == 2
        for receipt in source_receipts:
            artifact = await repository.get_raw_artifact(receipt.artifact_id)
            assert Path(artifact.file_path).read_bytes() == ACTIVE_NOTIFICATION
            assert artifact.mime_type == "application/soap+xml"
            assert artifact.sealed
    finally:
        await plugin.stop()
        await engine.stop()
        await repository.close()
