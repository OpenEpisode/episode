"""Developer-supplied vendor vocabularies behind the canonical ``event_type``.

The core classifies one vendor-neutral ``event_type`` (see
``episode.domain.event_filter``) and that class is the operator's only lever, so
a filter has to mean the same thing on every camera. Two properties are
required of a Device integration:

1. Its own recognisable signals resolve onto the *same* canonical type another
   integration uses for the same physical observation.
2. A signal it cannot name is never resolved at all. It is preserved and
   reported, but it does not become an ``Event`` with a name the core happens to
   classify into a filterable class.

Which vendor spellings a plugin may claim, and what each one means, belongs to
the plugin and to the vendor documentation the plugin's author read. This module
is where that claim is written down instead of being implied by a substring in
some parser, so ``tests/test_event_vocabulary.py`` can check that every canonical
name an integration declares it can emit is one the class model knows, that each
row cites the vendor documentation it came from, and that two spellings of one
signal do not land in different classes.

Nothing here interprets anything: the tables declare vocabulary, the adapters
still do the matching. They disagree on shape on purpose -- ONVIF identifies by
topic, Reolink by payload keyword, Hikvision by an XML ``eventType``, the SDK by
a numeric command -- but each row answers the same question: *which vendor name,
mapped to which canonical type, on whose documented authority*.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Placeholder a contributor must replace with a real citation. Rows still using
# it are carried as known debt (``tests/test_event_vocabulary.py`` pins the
# current list) rather than treated as evidence, so adding a row with it is a
# deliberate act a reviewer can see.
NEEDS_DOCUMENTATION = "needs-documentation"

# Canonical types that exist for non-camera reasons: the UI records a manual
# trigger, and ``unrecognized`` is what an integration emits when it cannot tell
# what a message means. Neither is a vendor vocabulary target, so the parity
# check skips them when asking "does every camera express this signal?".
CORE_ONLY_CANONICAL_TYPES: frozenset[str] = frozenset(
    {
        "manual_trigger",
        "unrecognized",
    }
)


@dataclass(frozen=True)
class VendorTerm:
    """One vendor spelling, what it means, and who said so."""

    vendor_value: str
    canonical_type: str
    source: str
    notes: str = ""


@dataclass(frozen=True)
class VendorVocabulary:
    """A plugin's claim about the names it can recognise."""

    vendor: str
    integration_id: str
    # How the plugin identifies events at all, so a contributor filling in a row
    # knows what a ``vendor_value`` is supposed to look like.
    identifies_by: str
    terms: tuple[VendorTerm, ...] = field(default_factory=tuple)
    # Names the plugin recognises through matching rather than a table lookup,
    # recorded so the parity check can still ask what the adapter can emit.
    recognised_canonical_types: tuple[str, ...] = field(default_factory=tuple)


HIKVISION_ISAPI = VendorVocabulary(
    vendor="hikvision",
    integration_id="hikvision:isapi",
    identifies_by="``eventType`` in ``EventNotificationAlert``; ``targetType`` "
    "refines a detection into a classified target",
    terms=(
        VendorTerm("VMD", "motion_detection", NEEDS_DOCUMENTATION),
        VendorTerm("videoloss", "video_loss", NEEDS_DOCUMENTATION),
        VendorTerm(
            "alarm",
            "digital_input",
            NEEDS_DOCUMENTATION,
            "plain event alert; carries no input id, so the terminal number is "
            "not lost by the mapping and stays verbatim as the vendor spelling",
        ),
        VendorTerm("human", "human_detection", NEEDS_DOCUMENTATION, "``targetType``"),
        VendorTerm("vehicle", "vehicle_detection", NEEDS_DOCUMENTATION, "``targetType``"),
    ),
    # Everything else is passed through verbatim in lower case, which the core
    # classifies as ``unknown``: never filtered, always preserved. That is the
    # documented behaviour for an unrecognised vendor name, not a gap.
    recognised_canonical_types=(
        "motion_detection",
        "video_loss",
        "digital_input",
        "human_detection",
        "vehicle_detection",
    ),
)

HIKVISION_SDK = VendorVocabulary(
    vendor="hikvision",
    integration_id="hikvision:sdk",
    identifies_by="HCNetSDK alarm/event command id plus subtype",
    terms=(
        VendorTerm("0x1133/17", "doorbell", NEEDS_DOCUMENTATION, "ringing subtype"),
        VendorTerm("1", "door_access", NEEDS_DOCUMENTATION, "``UNLOCK_RECORD``"),
    ),
    recognised_canonical_types=("doorbell", "door_access"),
)

ONVIF = VendorVocabulary(
    vendor="onvif",
    integration_id="onvif:events",
    identifies_by="WS-Notification topic, refined by ``SimpleItem`` names and values",
    terms=(
        VendorTerm(
            "tns1:RuleEngine/CellMotionDetector/Motion", "motion_detection", NEEDS_DOCUMENTATION
        ),
        VendorTerm("tns1:VideoSource/MotionAlarm", "motion_detection", NEEDS_DOCUMENTATION),
        VendorTerm(
            "tns1:RuleEngine/TamperDetector/Tamper", "tamper_detection", NEEDS_DOCUMENTATION
        ),
        # A digital input channel and its state topic: the same wired-contact
        # observation Hikvision reports as ``alarm``, so both must land on
        # ``digital_input`` or one selection reaches one camera and not the other.
        # ``Trigger/DigitalInput`` is the older spelling still advertised by some
        # firmwares; both mean the same physical input.
        VendorTerm("tns1:Device/DIInput/DIInputStatus", "digital_input", NEEDS_DOCUMENTATION),
        VendorTerm("tns1:Device/Trigger/DigitalInput", "digital_input", NEEDS_DOCUMENTATION),
    ),
    # A topic that reaches none of these is left uninterpreted: preserved,
    # counted on Device status, and never an ``Event`` the core could classify.
    recognised_canonical_types=(
        "human_detection",
        "vehicle_detection",
        "tamper_detection",
        "motion_detection",
        "audio_detection",
        "digital_input",
    ),
)

REOLINK = VendorVocabulary(
    vendor="reolink",
    integration_id="reolink:events",
    identifies_by="Baichuan frame command id, then an alarm payload keyword "
    "(``cmd``, ``AItype``, ``type``)",
    terms=(
        VendorTerm("audio", "audio_detection", NEEDS_DOCUMENTATION),
        VendorTerm("face", "face_detection", NEEDS_DOCUMENTATION),
        VendorTerm("human", "human_detection", NEEDS_DOCUMENTATION),
        VendorTerm("ladder", "ladder_detection", NEEDS_DOCUMENTATION),
        VendorTerm("line", "line_crossing_detection", NEEDS_DOCUMENTATION),
        VendorTerm("loitering", "loitering_detection", NEEDS_DOCUMENTATION),
        VendorTerm("motion", "motion_detection", NEEDS_DOCUMENTATION),
        VendorTerm("package", "package_detection", NEEDS_DOCUMENTATION),
        VendorTerm("pet", "pet_detection", NEEDS_DOCUMENTATION),
        VendorTerm("vehicle", "vehicle_detection", NEEDS_DOCUMENTATION),
        VendorTerm("vt", "tampering_detection", NEEDS_DOCUMENTATION, "vendor tamper marker"),
        VendorTerm("doorbell", "doorbell", NEEDS_DOCUMENTATION),
    ),
    recognised_canonical_types=(
        "audio_detection",
        "face_detection",
        "human_detection",
        "ladder_detection",
        "line_crossing_detection",
        "loitering_detection",
        "motion_detection",
        "package_detection",
        "pet_detection",
        "vehicle_detection",
        "tampering_detection",
        "doorbell",
        "battery_low",
        "battery_status",
    ),
)

BUILT_IN_VOCABULARIES: tuple[VendorVocabulary, ...] = (
    HIKVISION_ISAPI,
    HIKVISION_SDK,
    ONVIF,
    REOLINK,
)


def pending_documentation(
    vocabularies: tuple[VendorVocabulary, ...] = BUILT_IN_VOCABULARIES,
) -> list[str]:
    """Rows still missing a vendor-documentation citation, as ``integration:vendor``."""
    return [
        f"{vocabulary.integration_id}:{term.vendor_value}"
        for vocabulary in vocabularies
        for term in vocabulary.terms
        if not term.source or term.source == NEEDS_DOCUMENTATION
    ]
