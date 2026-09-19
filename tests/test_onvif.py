from __future__ import annotations

import os
import tempfile
import uuid
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from episode.actions.snapshot import SnapshotEngine
from episode.config import EpisodeConfig
from episode.domain.models import Area, Device, EventState
from episode.engine.bus import EventBus, Message
from episode.engine.engine import EpisodeEngine
from episode.media import CameraMedia, MediaRegistry
from episode.plugins.onvif.client import SOAP, TDS, WSA, ONVIFClient, ONVIFError
from episode.plugins.onvif.events import ONVIFStateTracker, parse_notifications
from episode.storage.repository import Repository

NOTIFICATIONS = b"""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
 xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2"
 xmlns:tt="http://www.onvif.org/ver10/schema">
 <s:Body><PullMessagesResponse>
  <wsnt:NotificationMessage>
   <wsnt:Topic>tns1:RuleEngine/CellMotionDetector/Motion</wsnt:Topic>
   <wsnt:Message><tt:Message UtcTime="2026-07-23T13:25:26Z"
     PropertyOperation="Initialized"><tt:Data>
    <tt:SimpleItem Name="IsMotion" Value="false"/>
   </tt:Data></tt:Message></wsnt:Message>
  </wsnt:NotificationMessage>
  <wsnt:NotificationMessage>
   <wsnt:Topic>tns1:RuleEngine/TamperDetector/Tamper</wsnt:Topic>
   <wsnt:Message><tt:Message UtcTime="2026-07-23T13:26:00Z"
     PropertyOperation="Changed"><tt:Data>
    <tt:SimpleItem Name="IsTamper" Value="true"/>
   </tt:Data></tt:Message></wsnt:Message>
  </wsnt:NotificationMessage>
 </PullMessagesResponse></s:Body>
</s:Envelope>"""

MALFORMED_GET_SERVICES_RESPONSE = b"""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
 xmlns:tds="http://www.onvif.org/ver10/device/wsdl"
 xmlns:tan="http://www.onvif.org/ver20/analytics/wsdl"
 xmlns:tev="http://www.onvif.org/ver10/events/wsdl">
 <s:Body><tds:GetServicesResponse>
  <tds:Service>
   <tds:Namespace>http://www.onvif.org/ver20/analytics/wsdl</tds:Namespace>
   <tds:XAddr>http://192.0.2.15:8000/onvif/analytics_service</tds:XAddr>
   <tds:Capabilities><tad:Capabilities RuleSupport="true"/></tds:Capabilities>
  </tds:Service>
  <tds:Service>
   <tds:Namespace>http://www.onvif.org/ver10/events/wsdl</tds:Namespace>
   <tds:XAddr>http://192.0.2.15:8000/onvif/event_service</tds:XAddr>
   <tds:Capabilities><tev:Capabilities WSPullPointSupport="true"/></tds:Capabilities>
  </tds:Service>
 </tds:GetServicesResponse></s:Body>
</s:Envelope>"""


def test_onvif_notifications_are_normalized_without_losing_initial_state():
    notifications = parse_notifications(ET.fromstring(NOTIFICATIONS))

    assert len(notifications) == 2
    assert notifications[0].event_type == "motion_detection"
    assert notifications[0].event_state == EventState.INACTIVE
    assert notifications[0].is_initial_value is True
    assert notifications[1].event_type == "tamper_detection"
    assert notifications[1].event_state == EventState.ACTIVE
    assert notifications[1].timestamp == datetime(2026, 7, 23, 13, 26, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_ws_username_token_never_contains_plaintext_password():
    client = ONVIFClient("192.0.2.1", "camera-user", "camera-secret")
    operation = ET.Element("{urn:test}Read")

    envelope = client._envelope(operation, authenticated=True)

    assert b"camera-user" in envelope
    assert b"camera-secret" not in envelope
    assert b"PasswordDigest" in envelope
    await client.close()


@pytest.mark.asyncio
async def test_relaxed_xml_recovers_known_onvif_data_without_weakening_the_default():
    strict = ONVIFClient("192.0.2.1", "user", "password")
    relaxed = ONVIFClient("192.0.2.1", "user", "password", relaxed_xml=True)
    try:
        with pytest.raises(ONVIFError, match="invalid SOAP XML"):
            strict._parse_response(MALFORMED_GET_SERVICES_RESPONSE)

        root = relaxed._parse_response(MALFORMED_GET_SERVICES_RESPONSE)
        services = root.findall(f".//{{{TDS}}}Service")

        assert [
            (
                service.findtext(f"{{{TDS}}}Namespace"),
                service.findtext(f"{{{TDS}}}XAddr"),
            )
            for service in services
        ] == [
            (
                "http://www.onvif.org/ver20/analytics/wsdl",
                "http://192.0.2.15:8000/onvif/analytics_service",
            ),
            (
                "http://www.onvif.org/ver10/events/wsdl",
                "http://192.0.2.15:8000/onvif/event_service",
            ),
        ]
    finally:
        await strict.close()
        await relaxed.close()


@pytest.mark.asyncio
async def test_relaxed_xml_does_not_resolve_external_entities(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("must-not-be-read", encoding="utf-8")
    response = f"""<!DOCTYPE s:Envelope [
<!ENTITY secret SYSTEM "{secret.as_uri()}">
]>
<s:Envelope xmlns:s="{SOAP}" xmlns:tds="{TDS}">
 <s:Body><tds:GetDeviceInformationResponse>
  <tds:Manufacturer>&secret;</tds:Manufacturer>
 </tds:GetDeviceInformationResponse></s:Body>
</s:Envelope>""".encode()
    client = ONVIFClient("192.0.2.1", "user", "password", relaxed_xml=True)
    try:
        root = client._parse_response(response)
        manufacturer = root.find(f".//{{{TDS}}}Manufacturer")

        assert manufacturer is not None
        assert manufacturer.text in (None, "")
    finally:
        await client.close()


def test_media_registry_adds_encoded_credentials_to_discovered_rtsp_uri():
    source = CameraMedia(
        device_id="camera-1",
        stream_uri="rtsp://192.0.2.10/stream/main",
        username="viewer@example",
        password="a/b c",
    )

    assert source.authenticated_stream_uri() == (
        "rtsp://viewer%40example:a%2Fb%20c@192.0.2.10/stream/main"
    )


@pytest.mark.asyncio
async def test_snapshot_action_preserves_downloaded_bytes_as_episode_evidence():
    temp_dir = tempfile.mkdtemp()
    config = EpisodeConfig(
        data_dir=temp_dir,
        db_path=os.path.join(temp_dir, "episode.db"),
        episode_timeout=10,
    )
    repo = Repository(config)
    bus = EventBus()
    media = MediaRegistry()
    media.register(
        CameraMedia(device_id="camera-1", snapshot_uri="http://camera/snapshot", source="onvif")
    )

    expected = b"\xff\xd8\xff\xe0immutable-jpeg-evidence"

    async def fake_snapshot(device_id: str):
        assert device_id == "camera-1"
        return expected, "image/jpeg"

    media.fetch_snapshot = fake_snapshot
    episode_engine = EpisodeEngine(repo, bus, timeout=10)
    snapshot_engine = SnapshotEngine(bus, media, config.data_dir)
    await repo.initialize()
    await repo.upsert_area(Area(id="entrance", name="Entrance"))
    await repo.upsert_device(
        Device(id="camera-1", name="Camera 1", device_type="camera", area_id="entrance")
    )
    await episode_engine.start()
    await snapshot_engine.start()

    await bus.publish(
        Message(
            type="event.received",
            data={
                "event": {
                    "device_id": "camera-1",
                    "area_id": "entrance",
                    "timestamp": datetime.now(timezone.utc),
                    "event_type": "motion_detection",
                    "event_state": "active",
                    "source": "onvif:events",
                }
            },
        )
    )

    for _ in range(20):
        evidence = await repo.list_evidence()
        if (
            evidence
            and evidence[0].episode_id is not None
            and os.path.exists(evidence[0].file_path)
        ):
            break
        import asyncio

        await asyncio.sleep(0.01)

    assert len(evidence) == 1
    assert evidence[0].episode_id is not None
    with open(evidence[0].file_path, "rb") as stored:
        assert stored.read() == expected
    receipts = await repo.list_ingestion_receipts(evidence_id=evidence[0].id)
    assert [receipt.source for receipt in receipts] == ["onvif:snapshot"]

    await snapshot_engine.stop()
    await episode_engine.stop()
    await repo.close()


@pytest.mark.asyncio
async def test_envelope_contains_ws_addressing_headers():
    client = ONVIFClient("192.0.2.1", "user", "pass")
    action = "http://www.onvif.org/ver10/events/wsdl/PullMessages"
    destination = "http://192.0.2.1/onvif/events"
    try:
        first = ET.fromstring(
            client._envelope(
                ET.Element("{urn:test}Read"),
                authenticated=False,
                soap_action=action,
                destination=destination,
            )
        )
        second = ET.fromstring(
            client._envelope(
                ET.Element("{urn:test}Read"),
                authenticated=False,
                soap_action=action,
                destination=destination,
            )
        )

        assert first.findtext(f"./{{{SOAP}}}Header/{{{WSA}}}To") == destination
        assert first.findtext(f"./{{{SOAP}}}Header/{{{WSA}}}Action") == action
        first_message_id = first.findtext(f"./{{{SOAP}}}Header/{{{WSA}}}MessageID")
        second_message_id = second.findtext(f"./{{{SOAP}}}Header/{{{WSA}}}MessageID")
        assert first_message_id and first_message_id.startswith("urn:uuid:")
        assert second_message_id and second_message_id.startswith("urn:uuid:")
        uuid.UUID(first_message_id.removeprefix("urn:uuid:"))
        uuid.UUID(second_message_id.removeprefix("urn:uuid:"))
        assert first_message_id != second_message_id
    finally:
        await client.close()


def test_onvif_level_notifications_only_emit_real_transitions():
    initial, active = parse_notifications(ET.fromstring(NOTIFICATIONS))
    tracker = ONVIFStateTracker()

    assert tracker.is_transition(initial) is False
    assert tracker.is_transition(initial) is False
    assert tracker.is_transition(active) is True
    assert tracker.is_transition(active) is False

    inactive = replace(active, event_state=EventState.INACTIVE)
    assert tracker.is_transition(inactive) is True
