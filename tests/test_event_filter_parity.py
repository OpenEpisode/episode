"""Cross-camera filter parity (``goose/Plan-EventFilter.md`` §18).

An Event Filter is one operator decision, so it must mean the same thing and
cost the same evidence whichever camera produced the observation. These tests
pin the two properties that make that true:

1. Every built-in Device integration maps its own recognisable signals onto the
   *same* vendor-neutral ``event_type``, and none of them falls back into a
   filterable class when it does not recognise a message.
2. Under one Capture Profile or Device selector, the same canonical
   ``event_type`` produces the same class, the same decision, the same
   ``filter_source`` and the same attachment outcome regardless of the Device
   that emitted it.

The vendor payloads are sanitized fixtures of the shapes already captured in the
per-adapter tests; they are not live-camera recordings.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from episode.capture_profiles import CaptureProfileService
from episode.config import EpisodeConfig
from episode.domain.event_filter import (
    ATTACHMENT_ATTACHED,
    EVENT_CLASS_CONDITION,
    EVENT_CLASS_DETECTION,
    EVENT_CLASS_HEARTBEAT,
    EVENT_CLASS_MOTION,
    EVENT_CLASS_SECURITY,
    EVENT_CLASS_UNKNOWN,
    EVENT_CLASSES,
    FILTER_SOURCE_PROFILE,
    FILTERED_REASON,
    UNRECOGNIZED_EVENT_TYPE,
    event_class,
    is_filterable,
)
from episode.domain.models import (
    Area,
    Device,
    EpisodeState,
    Event,
    EventState,
    IngestionReceipt,
)
from episode.engine.bus import EventBus
from episode.engine.engine import EpisodeEngine
from episode.plugins.hikvision.xml_events import HikvisionEvent
from episode.plugins.onvif.events import parse_notifications
from episode.plugins.reolink.events import interpret_event, parse_battery_status_frame
from episode.storage.repository import Repository

# Every class except ``unknown``. Selecting ``unknown`` as well is legal, but it
# would also suppress the unnamed leftovers these tests use to prove that an
# adapter has not guessed, so the widest selector used here names known classes.
NAMED_CLASS_SELECTOR = EVENT_CLASSES - {EVENT_CLASS_UNKNOWN}
WIDEST_PROFILE_SELECTOR = sorted(NAMED_CLASS_SELECTOR)

# One Area and one Device per integration, so a difference in behaviour cannot
# be explained away by a different Area or a different profile membership.
VENDOR_SOURCES = {
    "hikvision-isapi": "hikvision:isapi",
    "hikvision-alarm": "hikvision:alarm_server",
    "onvif": "onvif:events",
    "reolink": "reolink:events",
}


def hikvision_event_type(xml: bytes) -> str:
    parsed = HikvisionEvent.from_bytes(xml)
    assert parsed is not None
    return parsed.event_type


def onvif_event_type(topic: str, name: str, value: str) -> str:
    xml = f"""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
 xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2"
 xmlns:tt="http://www.onvif.org/ver10/schema">
 <s:Body><PullMessagesResponse>
  <wsnt:NotificationMessage>
   <wsnt:Topic>{topic}</wsnt:Topic>
   <wsnt:Message><tt:Message UtcTime="2026-08-14T10:41:18Z"
     PropertyOperation="Changed"><tt:Data>
    <tt:SimpleItem Name="{name}" Value="{value}"/>
   </tt:Data></tt:Message></wsnt:Message>
  </wsnt:NotificationMessage>
 </PullMessagesResponse></s:Body>
</s:Envelope>""".encode()
    notifications = parse_notifications(ET.fromstring(xml))
    assert len(notifications) == 1
    return notifications[0].event_type


def hikvision_alarm_xml(event_type: str, target_type: str = "") -> bytes:
    target = (
        f"<DetectionRegionList><DetectionRegionEntry><targetType>{target_type}</targetType>"
        "</DetectionRegionEntry></DetectionRegionList>"
        if target_type
        else ""
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<EventNotificationAlert xmlns="http://www.hikvision.com/ver20/XMLSchema">
  <ipAddress>192.0.2.10</ipAddress>
  <dateTime>2026-08-14T10:41:18+00:00</dateTime>
  <eventType>{event_type}</eventType>
  <eventState>active</eventState>
  <channelName>Gate camera</channelName>
  {target}
</EventNotificationAlert>""".encode()


# --- One canonical vocabulary per signal, across every adapter ---------------


def test_the_same_signal_canonicalizes_identically_from_every_vendor():
    """Vendor keyword ownership stays in the adapter; the canonical name does not
    vary. A profile's class decision is only comparable across cameras while
    equivalent signals share one type."""
    assert hikvision_event_type(hikvision_alarm_xml("VMD")) == "motion_detection"
    assert (
        onvif_event_type("tns1:RuleEngine/CellMotionDetector/Motion", "IsMotion", "true")
        == "motion_detection"
    )
    assert interpret_event({"cmd": "MotionDetect", "state": "true"}).event_type == (
        "motion_detection"
    )

    assert hikvision_event_type(hikvision_alarm_xml("alarm", "human")) == "human_detection", (
        "Hikvision reports a classified target via targetType"
    )
    assert onvif_event_type("tns1:RuleEngine/SmartFrame/HumanDetection", "Class", "Human") == (
        "human_detection"
    )
    assert interpret_event({"AItype": "Human", "status": "true"}).event_type == "human_detection"

    assert hikvision_event_type(hikvision_alarm_xml("videoloss")) == "video_loss"
    assert onvif_event_type("tns1:RuleEngine/TamperDetector/Tamper", "IsTamper", "true") == (
        "tamper_detection"
    )


def test_a_wired_contact_reaches_one_canonical_type_on_every_adapter():
    """A wired alarm input is one operator-authored signal, whatever the vendor.

    Hikvision reports it as ``alarm`` on the alert stream and ONVIF as a digital
    input channel or ``DIInput``/``DIInputStatus`` topic. Both must canonicalize
    to ``digital_input``, or one camera's wired trigger is suppressible and its
    neighbour's is not. It lands in ``detection`` with the other
    operator-authored rules: it asserts nothing about the camera, and which
    terminal means what is a deployment choice.
    """
    hikvision_contact = hikvision_event_type(hikvision_alarm_xml("alarm"))
    onvif_state = onvif_event_type("tns1:Device/DIInput/DIInputStatus", "state", "true")
    onvif_property = onvif_event_type("tns1:Device/Trigger/DigitalInput", "DigitalInput", "true")
    assert hikvision_contact == "digital_input"
    assert onvif_state == "digital_input"
    assert onvif_property == "digital_input"
    assert event_class(hikvision_contact) == event_class(onvif_state) == EVENT_CLASS_DETECTION
    assert event_class(onvif_property) == EVENT_CLASS_DETECTION
    # A classified target still refines through ``targetType`` rather than being
    # flattened into the wired-contact type.
    assert hikvision_event_type(hikvision_alarm_xml("alarm", "human")) == "human_detection"
    # An unnamed topic is still not an Event, so a selector cannot silence it.
    assert onvif_event_type("tns1:VendorCustom/UnnamedDetector", "State", "true") is None
    assert is_filterable(None, NAMED_CLASS_SELECTOR) is False


def test_no_adapter_invents_a_canonical_type_for_an_unnamed_signal():
    """An unrecognized message must never land in a filterable class.

    Otherwise the same physical signal could be suppressed on one camera and not
    another, purely because that vendor's firmware had no keyword the adapter
    knew. Unrecognized stays ``unknown``, reachable only by an operator who
    selects ``unknown`` deliberately, and is preserved either way.
    """
    hikvision_unknown = hikvision_event_type(hikvision_alarm_xml("firealarm"))
    assert hikvision_unknown == "firealarm", "the vendor spelling survives verbatim"
    assert event_class(hikvision_unknown) == EVENT_CLASS_UNKNOWN
    assert is_filterable(hikvision_unknown, NAMED_CLASS_SELECTOR) is False

    reolink_unknown = interpret_event({"status": "none", "channelId": 0}).event_type
    assert reolink_unknown == UNRECOGNIZED_EVENT_TYPE
    assert event_class(reolink_unknown) == EVENT_CLASS_UNKNOWN
    assert is_filterable(reolink_unknown, NAMED_CLASS_SELECTOR) is False
    assert is_filterable(reolink_unknown, {EVENT_CLASS_UNKNOWN}) is True, (
        "it is suppressible, but only by naming the class that means 'unnamed'"
    )

    # ONVIF does not guess either: a topic it cannot name produces no Event at
    # all, so it can never be filtered into silence. That asymmetry with the
    # other two adapters is tracked in §18 and is asserted below.
    assert onvif_event_type("tns1:VendorCustom/UnnamedDetector", "State", "true") is None


def test_device_integrations_agree_on_the_device_status_vocabulary():
    """A battery *report* is bookkeeping and a *low* battery is a security
    signal, and every integration lands each of them in the same class, so one
    selection cannot mute one camera's battery state and keep another's."""
    report = parse_battery_status_frame(
        b"<batteryStatus><batteryPower>85</batteryPower>"
        b"<isCharging>false</isCharging></batteryStatus>",
        channel=0,
    )
    assert report is not None
    assert report.event_type == "battery_status"
    assert event_class(report.event_type) == EVENT_CLASS_HEARTBEAT
    assert is_filterable(report.event_type, {EVENT_CLASS_HEARTBEAT}) is True
    assert is_filterable(report.event_type, {EVENT_CLASS_CONDITION}) is False

    # A low battery is the same kind of observation on every camera, and it is a
    # condition rather than routine bookkeeping.
    low = parse_battery_status_frame(
        b"<batteryStatus><batteryPower>5</batteryPower>"
        b"<isCharging>false</isCharging></batteryStatus>",
        channel=0,
    )
    assert low is not None
    assert low.event_type == "battery_low"
    assert event_class(low.event_type) == EVENT_CLASS_SECURITY
    assert is_filterable(low.event_type, {EVENT_CLASS_SECURITY}) is True
    assert is_filterable(low.event_type, {EVENT_CLASS_HEARTBEAT}) is False


@pytest.mark.parametrize(
    ("xml", "expected"),
    [
        (b"<Event><cmd>VtDetect</cmd><state>true</state></Event>", "tampering_detection"),
        (b"<Event><cmd>TamperDetect</cmd><state>true</state></Event>", "tampering_detection"),
    ],
)
def test_reolink_tamper_reaches_the_security_class_as_any_other_camera_tamper(xml, expected):
    """Tamper is suppressible, so it has to be a class the operator can actually
    see and name: a vendor spelling an adapter failed to recognize would have
    left it ``unknown``, reachable only by selecting that class, and a profile
    suppressing ``security`` would have missed it on this camera alone."""
    parsed = interpret_event({"cmd": xml.split(b"<cmd>")[1].split(b"</cmd>")[0].decode()})
    assert parsed.event_type == expected
    assert event_class(parsed.event_type) == EVENT_CLASS_SECURITY


# --- One selector, one outcome, whatever the camera --------------------------


@pytest_asyncio.fixture
async def parity_context(tmp_path):
    repo = Repository(EpisodeConfig(data_dir=str(tmp_path), db_path=str(tmp_path / "db.sqlite3")))
    await repo.initialize()
    for index, (device_id, source) in enumerate(VENDOR_SOURCES.items()):
        await repo.upsert_area(Area(id=f"area-{index}", name=f"Area {index}"))
        await repo.upsert_device(
            Device(
                id=device_id,
                name=f"{source} camera",
                device_type="camera",
                area_id=f"area-{index}",
                # A short window lets the lifecycle pass reach QUIESCENT inside the
                # test's own clock, which is what the attach-not-drive assertions
                # need. It is identical for every Device, so it cannot explain a
                # difference between vendors.
                activity_window_seconds=1,
            )
        )
    yield repo, CaptureProfileService(repo)
    await repo.close()


def parity_event(device_id: str, event_type: str, **kwargs) -> Event:
    return Event(
        device_id=device_id,
        area_id=f"area-{list(VENDOR_SOURCES).index(device_id)}",
        event_type=event_type,
        event_state=EventState.ACTIVE,
        source=VENDOR_SOURCES[device_id],
        **kwargs,
    )


async def stored_receipt(repo: Repository, received_at: datetime) -> IngestionReceipt:
    """Lifecycle uses the Receipt's arrival time, so the attach test needs one."""
    receipt = IngestionReceipt(source="test", received_at=received_at)
    await repo.create_ingestion_receipt(receipt)
    return receipt


@pytest.mark.asyncio
async def test_one_profile_selector_decides_every_camera_the_same_way(parity_context):
    repo, service = parity_context
    profile = await service.create_profile(
        "Quiet", list(VENDOR_SOURCES), event_filter=WIDEST_PROFILE_SELECTOR
    )
    await repo.activate_capture_profile(profile.id, source="test")

    # The widest selector names every known class, so every known type is
    # suppressed -- including the security and detection classes that an earlier
    # revision protected at profile level. What survives is only what the
    # selector did not name: unnamed observations, because `unknown` is not in
    # this selector. The point of the assertion is not which side each type
    # lands on but that all four cameras land on the same side.
    for event_type, expected_allowed in (
        ("motion_detection", False),
        ("system", False),
        ("battery_status", False),
        ("battery_low", False),
        ("audio_detection", False),
        ("tamper_detection", False),
        ("video_loss", False),
        ("digital_input", False),
        ("human_detection", False),
        ("door_access", False),
        ("firealarm", True),
        (UNRECOGNIZED_EVENT_TYPE, True),
    ):
        decisions = []
        for device_id in VENDOR_SOURCES:
            decision, _targets = await service.evaluate_event(parity_event(device_id, event_type))
            decisions.append(decision)
            assert decision.allowed is expected_allowed, (device_id, event_type)
        first = decisions[0]
        # The reason, the recorded class, and the deciding level must match too:
        # "same impact" includes the audit trail, not just the yes/no.
        assert {(d.allowed, d.reason) for d in decisions} == {(expected_allowed, first.reason)}
        assert {d.filtered_event_class for d in decisions} == {first.filtered_event_class}
        assert {d.filter_source for d in decisions} == {first.filter_source}
        if not expected_allowed:
            assert first.reason == FILTERED_REASON
            assert first.filter_source == FILTER_SOURCE_PROFILE
            assert first.filtered_event_type == event_type


@pytest.mark.asyncio
async def test_a_device_opt_out_only_changes_that_device(parity_context):
    """Precedence is per-camera: opting one lens out of a filtering profile must
    not change the meaning of that profile for the others."""
    repo, service = parity_context
    profile = await service.create_profile(
        "Quiet", list(VENDOR_SOURCES), event_filter=[EVENT_CLASS_MOTION]
    )
    await repo.activate_capture_profile(profile.id, source="test")
    await repo.upsert_device(
        Device(
            id="reolink",
            name="Reolink camera",
            device_type="camera",
            area_id="area-3",
            event_filter=[],
        )
    )

    filtered, _ = await service.evaluate_event(parity_event("onvif", "motion_detection"))
    opted_out, _ = await service.evaluate_event(parity_event("reolink", "motion_detection"))
    assert filtered.allowed is False and filtered.filter_source == FILTER_SOURCE_PROFILE
    assert opted_out.allowed is True
    assert opted_out.reason != FILTERED_REASON

    # A camera that selects a class a profile does not also filters alone.
    await repo.upsert_device(
        Device(
            id="onvif",
            name="ONVIF camera",
            device_type="camera",
            area_id="area-2",
            event_filter=[EVENT_CLASS_SECURITY],
        )
    )
    tamper, _ = await service.evaluate_event(parity_event("onvif", "tamper_detection"))
    other, _ = await service.evaluate_event(parity_event("hikvision-isapi", "tamper_detection"))
    assert tamper.allowed is False and tamper.filter_source == "device"
    assert other.allowed is True


@pytest.mark.asyncio
async def test_a_filtered_event_attaches_identically_for_every_camera(parity_context):
    """Attach-not-drive is defined once, so it must behave identically per vendor:
    attributed to the open Episode, no deadline change, no state change, no
    Evidence, and never a reopen of a quiescent Episode."""
    repo, service = parity_context
    profile = await service.create_profile(
        "Quiet", list(VENDOR_SOURCES), event_filter=[EVENT_CLASS_MOTION]
    )
    await repo.activate_capture_profile(profile.id, source="test")
    engine = EpisodeEngine(repo, EventBus(), timeout=30, capture_profiles=service)
    await engine.start()
    try:
        base = datetime.now(timezone.utc) - timedelta(seconds=4)
        for device_id in VENDOR_SOURCES:
            opening = await engine.ingest_event(
                parity_event(device_id, "human_detection", timestamp=base),
                receipt=await stored_receipt(repo, base),
            )
            episode_id = opening.event.episode_id
            assert episode_id is not None, device_id
            await repo.transition_timed_out_episodes(
                timeout=30,
                quiescent_grace_seconds=5,
                now=base + timedelta(seconds=2),
            )
            before = await repo.get_episode(episode_id)
            assert before.state == EpisodeState.QUIESCENT, device_id

            filtered = await engine.ingest_event(
                parity_event(device_id, "motion_detection", timestamp=base + timedelta(seconds=3)),
                receipt=await stored_receipt(repo, base + timedelta(seconds=3)),
            )
            stored = await repo.get_event(filtered.event.id)
            after = await repo.get_episode(episode_id)

            assert stored.episode_id == episode_id, device_id
            assert stored.participation.attachment == ATTACHMENT_ATTACHED, device_id
            assert after.state == EpisodeState.QUIESCENT, device_id
            assert after.minimum_end_at == before.minimum_end_at, device_id
            assert after.last_activity_at == before.last_activity_at, device_id
            assert await repo.list_evidence(episode_id=episode_id) == [], device_id
    finally:
        await engine.stop()
