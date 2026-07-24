from __future__ import annotations

import os
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from episode.actions.snapshot import SnapshotEngine
from episode.config import EpisodeConfig
from episode.connectors.onvif.client import ONVIFClient
from episode.connectors.onvif.parser import ONVIFStateTracker, parse_notifications
from episode.domain.models import EventState
from episode.engine.bus import EventBus, Message
from episode.engine.engine import EpisodeEngine
from episode.media import CameraMedia, MediaRegistry
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
    media.register(CameraMedia(device_id="camera-1", snapshot_uri="http://camera/snapshot"))

    expected = b"\xff\xd8\xff\xe0immutable-jpeg-evidence"

    async def fake_snapshot(device_id: str):
        assert device_id == "camera-1"
        return expected, "image/jpeg"

    media.fetch_snapshot = fake_snapshot
    episode_engine = EpisodeEngine(repo, bus, timeout=10)
    snapshot_engine = SnapshotEngine(bus, media, config)
    await repo.initialize()
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
        if evidence and os.path.exists(evidence[0].file_path):
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


def test_onvif_level_notifications_only_emit_real_transitions():
    initial, active = parse_notifications(ET.fromstring(NOTIFICATIONS))
    tracker = ONVIFStateTracker()

    assert tracker.is_transition(initial) is False
    assert tracker.is_transition(initial) is False
    assert tracker.is_transition(active) is True
    assert tracker.is_transition(active) is False

    inactive = replace(active, event_state=EventState.INACTIVE)
    assert tracker.is_transition(inactive) is True
