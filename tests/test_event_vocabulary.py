"""The vendor vocabulary contract (``goose/Plan-EventFilter.md`` §18).

A filter is one operator decision, so it has to reach the same signal on every
camera. That depends on each Device integration handing the core a canonical
name the core actually knows: an adapter that invents a name, or that carries on
using one that has drifted out of ``EVENT_CLASS_BY_TYPE``, produces an Event that
silently lands in ``unknown`` -- preserved, but unreachable by any class the
operator selected, and reachable on a neighbouring camera that used the proper
spelling.

These tests do not interpret vendor traffic and do not assert what a vendor
documentation says. The vendor vocabularies are declared by the developers who
read them, in ``episode.plugins.event_vocabulary``. What is checked here is the
shape of that declaration:

* every canonical type an adapter can emit is classified by the core, so no
  adapter's private name escapes the class model;
* every declared row cites vendor documentation, and a new row without a
  citation fails rather than joins the accepted-debt list quietly;
* spellings that denote the same physical signal agree on class, so one
  camera's tamper cannot sit in a different class from another's while the
  names still differ.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from episode.domain.event_filter import (
    EVENT_CLASS_BY_TYPE,
    UNRECOGNIZED_EVENT_TYPE,
    event_class,
)
from episode.plugins.event_vocabulary import (
    BUILT_IN_VOCABULARIES,
    CORE_ONLY_CANONICAL_TYPES,
    NEEDS_DOCUMENTATION,
    pending_documentation,
)
from episode.plugins.hikvision.xml_events import HikvisionEvent
from episode.plugins.onvif.events import parse_notifications
from episode.plugins.reolink.events import interpret_event


def _emittable_types():
    for vocabulary in BUILT_IN_VOCABULARIES:
        for canonical_type in vocabulary.recognised_canonical_types:
            yield vocabulary.integration_id, canonical_type


def test_every_name_an_adapter_can_emit_is_classified_by_the_core():
    """An adapter may not hand the core a name the class model has never heard.

    Such a type is preserved but unclassifiable, so an operator selecting its
    class on another camera suppresses it there and not here -- the same
    observation with two filter outcomes. Adapters use
    ``UNRECOGNIZED_EVENT_TYPE`` when they cannot name a message instead.
    """
    unknown_names = sorted(
        f"{integration_id} -> {canonical_type}"
        for integration_id, canonical_type in _emittable_types()
        if canonical_type not in EVENT_CLASS_BY_TYPE and canonical_type != UNRECOGNIZED_EVENT_TYPE
    )
    assert unknown_names == []


@pytest.mark.parametrize("vocabulary", BUILT_IN_VOCABULARIES, ids=lambda item: item.integration_id)
def test_a_vocabulary_declares_what_it_recognises_and_how(vocabulary):
    """A contributor filling in vendor terms must be able to tell what a
    ``vendor_value`` is, and the table must not be empty by accident."""
    assert vocabulary.identifies_by
    assert vocabulary.recognised_canonical_types, (
        f"{vocabulary.integration_id} declares no recognised types, so it can only"
        ' ever emit "unrecognized"'
    )
    assert set(vocabulary.recognised_canonical_types).isdisjoint(CORE_ONLY_CANONICAL_TYPES), (
        "a vendor vocabulary may not claim a type that exists for core reasons"
    )


def test_declared_rows_point_at_their_canonical_target():
    for vocabulary in BUILT_IN_VOCABULARIES:
        for term in vocabulary.terms:
            assert term.vendor_value.strip() == term.vendor_value
            assert term.canonical_type in vocabulary.recognised_canonical_types, (
                f"{vocabulary.integration_id}:{term.vendor_value} maps to a type the "
                "adapter does not declare it can emit"
            )


def test_every_vendor_row_carries_a_documentation_citation():
    """Vendor meaning is asserted by a developer who read the vendor, not by a
    contributor guessing from a payload.

    Rows carrying ``NEEDS_DOCUMENTATION`` are the accepted-debt list for this
    release. The assertion is exact, so adding a row without a citation grows
    the list and fails here: the only way past is to supply the source, or to
    add the row to that list knowingly.
    """
    assert pending_documentation() == [
        "hikvision:isapi:VMD",
        "hikvision:isapi:videoloss",
        "hikvision:isapi:alarm",
        "hikvision:isapi:human",
        "hikvision:isapi:vehicle",
        "hikvision:sdk:0x1133/17",
        "hikvision:sdk:1",
        "onvif:events:tns1:RuleEngine/CellMotionDetector/Motion",
        "onvif:events:tns1:VideoSource/MotionAlarm",
        "onvif:events:tns1:RuleEngine/TamperDetector/Tamper",
        "onvif:events:tns1:Device/DIInput/DIInputStatus",
        "onvif:events:tns1:Device/Trigger/DigitalInput",
        "reolink:events:audio",
        "reolink:events:face",
        "reolink:events:human",
        "reolink:events:ladder",
        "reolink:events:line",
        "reolink:events:loitering",
        "reolink:events:motion",
        "reolink:events:package",
        "reolink:events:pet",
        "reolink:events:vehicle",
        "reolink:events:vt",
        "reolink:events:doorbell",
    ]


def test_no_row_is_marked_as_cited_by_copying_the_placeholder_wrong():
    for vocabulary in BUILT_IN_VOCABULARIES:
        for term in vocabulary.terms:
            assert term.source.strip() == term.source
            assert term.source, f"{vocabulary.integration_id}:{term.vendor_value} cites nothing"
            if term.source == NEEDS_DOCUMENTATION:
                continue
            assert len(term.source) > len(NEEDS_DOCUMENTATION)


# Canonical spellings that denote the same physical signal. Two integrations may
# name a signal differently and still be homogeneous only while both names carry
# one class; if they ever diverge, an operator's selection reaches one camera and
# not the other.
SIGNAL_SPELLINGS = {
    "tamper": ("tamper_detection", "tampering_detection"),
    "motion": ("motion_detection",),
    "video loss": ("video_loss",),
    "wired contact": ("digital_input",),
    "human": ("human_detection",),
    "vehicle": ("vehicle_detection",),
}


@pytest.mark.parametrize(("signal", "spellings"), sorted(SIGNAL_SPELLINGS.items()))
def test_spellings_of_one_signal_share_one_class(signal, spellings):
    classes = {event_class(name) for name in spellings}
    assert len(classes) == 1, f"{signal} is spelled {spellings} and classified {sorted(classes)}"


def hikvision_alarm_xml(event_type: str, target_type: str = "") -> bytes:
    target = (
        "<DetectionRegionList><DetectionRegionEntry>"
        f"<targetType>{target_type}</targetType>"
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


def _probe_hikvision_isapi(vendor_value: str) -> str:
    if vendor_value in {"human", "vehicle"}:
        parsed = HikvisionEvent.from_bytes(hikvision_alarm_xml("VMD", vendor_value))
    else:
        parsed = HikvisionEvent.from_bytes(hikvision_alarm_xml(vendor_value))
    assert parsed is not None
    return parsed.event_type


def _probe_onvif(vendor_value: str) -> str:
    xml = f"""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
 xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2"
 xmlns:tt="http://www.onvif.org/ver10/schema">
 <s:Body><PullMessagesResponse>
  <wsnt:NotificationMessage>
   <wsnt:Topic>{vendor_value}</wsnt:Topic>
   <wsnt:Message><tt:Message UtcTime="2026-08-14T10:41:18Z"
     PropertyOperation="Changed"><tt:Data>
    <tt:SimpleItem Name="State" Value="true"/>
   </tt:Data></tt:Message></wsnt:Message>
  </wsnt:NotificationMessage>
 </PullMessagesResponse></s:Body>
</s:Envelope>""".encode()
    notifications = parse_notifications(ET.fromstring(xml))
    assert len(notifications) == 1
    return notifications[0].event_type


def _probe_reolink(vendor_value: str) -> str:
    return interpret_event({"cmd": vendor_value, "status": "true"}).event_type


# A declared row that nothing executes is a comment, not a mapping. Each table
# row is run through its real adapter and must yield exactly the canonical type
# the developer claimed, otherwise the vocabulary and the parser have drifted.
PROBES = {
    "hikvision:isapi": _probe_hikvision_isapi,
    "onvif:events": _probe_onvif,
    "reolink:events": _probe_reolink,
}


@pytest.mark.parametrize("vocabulary", BUILT_IN_VOCABULARIES, ids=lambda item: item.integration_id)
def test_a_declared_row_is_what_the_adapter_actually_emits(vocabulary):
    probe = PROBES.get(vocabulary.integration_id)
    if probe is None:
        # The SDK matches numeric command ids that need a binary payload to
        # exercise; its rows are pinned by the dedicated adapter tests instead.
        assert vocabulary.integration_id == "hikvision:sdk"
        return
    for term in vocabulary.terms:
        assert probe(term.vendor_value) == term.canonical_type, (
            f"{vocabulary.integration_id}:{term.vendor_value} is declared as"
            f" {term.canonical_type} but the adapter emits something else"
        )


def test_the_undeclared_vendor_spelling_stays_out_of_every_class():
    """A name nobody mapped is ``unknown``, which is never filterable.

    This is what makes an incomplete vocabulary safe rather than merely wrong:
    an adapter that has not documented a vendor signal yet cannot accidentally
    let an operator mute it either.
    """
    assert event_class("firealarm") == "unknown"
    assert event_class(UNRECOGNIZED_EVENT_TYPE) == "unknown"
