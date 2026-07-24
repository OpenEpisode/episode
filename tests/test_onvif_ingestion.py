from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

from episode.connectors.onvif.connector import ONVIFConnector
from episode.connectors.onvif.parser import ONVIFStateTracker
from episode.engine.bus import EventBus

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


@pytest.mark.asyncio
async def test_repeated_state_keeps_raw_receipt_without_emitting_another_event(tmp_path):
    bus = EventBus()
    events = []
    receipts = []
    bus.subscribe("event.received", events.append)
    bus.subscribe("receipt.received", receipts.append)

    connector = object.__new__(ONVIFConnector)
    connector._app_config = SimpleNamespace(orphans_dir=str(tmp_path))
    connector._configured_device = SimpleNamespace(id="camera-1", area_id="entrance")
    connector._bus = bus
    connector._state_tracker = ONVIFStateTracker()
    connector._received = 0
    connector._suppressed = 0
    connector._last_event = None
    root = ET.fromstring(ACTIVE_NOTIFICATION)

    await connector._ingest_response(root, ACTIVE_NOTIFICATION)
    await connector._ingest_response(root, ACTIVE_NOTIFICATION)

    assert len(events) == 1
    assert len(receipts) == 1
    assert receipts[0].data["receipt"]["metadata"]["reason"] == "repeated_state"
    artifact = receipts[0].data["artifact"]
    assert Path(artifact["file_path"]).read_bytes() == ACTIVE_NOTIFICATION
    assert connector._received == 1
    assert connector._suppressed == 1
