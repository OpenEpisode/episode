"""Event classification for the capture-participation filter.

The core classifies canonical, vendor-neutral ``event_type`` strings into
semantic classes so an operator can suppress observations that should not drive
capture. This is pure domain logic and must not import plugins or vendor code.

Classes are the stable abstraction; plugins own vendor keyword mapping.

**Every class is filterable, at both levels.** A selector is a set of classes
and it applies uniformly: no class is privileged over another, and a Device
level is not more capable than a Capture Profile level. What filtering changes
is only whether an Event may *drive* capture -- a filtered Event never opens an
Episode and never extends one, but its Raw Artifact, Receipt and canonical Event
are still persisted, and it still joins an already open Episode as context.

Filtering is therefore never a deletion, and it never hides an observation from
the record. It removes only the power to start or lengthen recording.

Unknown event types classify as ``unknown``, which keeps the default safe: an
operator has to select ``unknown`` explicitly to suppress anything the build
cannot name.
"""

import json

EVENT_CLASS_MOTION = "motion"
EVENT_CLASS_HEARTBEAT = "heartbeat"
EVENT_CLASS_CONDITION = "condition"
EVENT_CLASS_SECURITY = "security"
EVENT_CLASS_DETECTION = "detection"
EVENT_CLASS_ACCESS = "access"
EVENT_CLASS_UNKNOWN = "unknown"

EVENT_CLASSES: frozenset[str] = frozenset(
    {
        EVENT_CLASS_MOTION,
        EVENT_CLASS_HEARTBEAT,
        EVENT_CLASS_CONDITION,
        EVENT_CLASS_SECURITY,
        EVENT_CLASS_DETECTION,
        EVENT_CLASS_ACCESS,
        EVENT_CLASS_UNKNOWN,
    }
)
"""Every class exists so it can be selected. There is no privileged class and no
class that is unconditionally immune: a selector means the same thing whatever
it names, and a Capture Profile is not less capable than a Device."""

FILTERABLE_EVENT_CLASSES: frozenset[str] = EVENT_CLASSES
PROFILE_SELECTABLE_EVENT_CLASSES: frozenset[str] = EVENT_CLASSES
DEVICE_SELECTABLE_EVENT_CLASSES: frozenset[str] = EVENT_CLASSES

EVENT_FILTER_LEVELS: tuple[str, ...] = ("profile", "device")

FILTER_SOURCE_DEVICE = "device"
FILTER_SOURCE_PROFILE = "profile"

ATTACHMENT_ATTACHED = "attached"
ATTACHMENT_NO_OPEN_EPISODE = "no_open_episode"

INHERIT_EVENT_FILTER = "inherit"
"""Storage sentinel for a Device with no override. Never a selector value."""

UNRECOGNIZED_EVENT_TYPE = "unrecognized"
"""Canonical type a Device integration uses when it cannot tell what a message
means. It classifies as ``unknown``, so suppressing it is an explicit operator
choice rather than a side effect of a preset, and it stays preserved and
queryable either way. Integrations must not fall back to a name from another
class (``system``, for example) because that would let one camera's noise be
suppressed while an identical signal from another camera could not be, and would
hide an unidentified observation behind a ``heartbeat`` selection."""

FILTERED_REASON = "generic_event_filtered"
"""Stable participation reason for a class-filtered Event.

The name is retained from ``beta.7`` so stored rows, UI badges and docs stay
valid; ``filtered_event_class`` and ``filter_source`` disambiguate the decision.
"""

EVENT_CLASS_BY_TYPE: dict[str, str] = {
    # Passive scene change with no classification and no security assertion.
    "motion_detection": EVENT_CLASS_MOTION,
    # Device bookkeeping: no asserted incident.
    "system": EVENT_CLASS_HEARTBEAT,
    "battery_status": EVENT_CLASS_HEARTBEAT,
    # Camera-reported condition about its scene, neither a target nor a rule.
    "audio_detection": EVENT_CLASS_CONDITION,
    # The observation is the incident, or reports loss of the ability to see.
    "tamper_detection": EVENT_CLASS_SECURITY,
    "tampering_detection": EVENT_CLASS_SECURITY,
    "video_loss": EVENT_CLASS_SECURITY,
    "battery_low": EVENT_CLASS_SECURITY,
    # A classified target, or an operator-authored rule carrying intent. A wired
    # contact is the latter: the camera reports that a signal the operator
    # chose to raise has been raised, and which physical input it was is a
    # deployment fact written into the camera, not a property of the signal.
    "digital_input": EVENT_CLASS_DETECTION,
    "human_detection": EVENT_CLASS_DETECTION,
    "vehicle_detection": EVENT_CLASS_DETECTION,
    "pet_detection": EVENT_CLASS_DETECTION,
    "animal_detection": EVENT_CLASS_DETECTION,
    "face_detection": EVENT_CLASS_DETECTION,
    "ladder_detection": EVENT_CLASS_DETECTION,
    "package_detection": EVENT_CLASS_DETECTION,
    "line_crossing_detection": EVENT_CLASS_DETECTION,
    "loitering_detection": EVENT_CLASS_DETECTION,
    "doorbell": EVENT_CLASS_DETECTION,
    "manual_trigger": EVENT_CLASS_DETECTION,
    # Physical access records are evidentiary by nature.
    "door_access": EVENT_CLASS_ACCESS,
    "card": EVENT_CLASS_ACCESS,
}
"""Canonical ``event_type`` to class. Anything else classifies as ``unknown``."""

# ``beta.7`` exposed one flat "generic" list. This is the class equivalent of
# that behaviour and is used only to translate legacy API fields and the legacy
# stored columns. It deliberately excludes ``security``, and it also excludes
# ``detection``, so a wired contact (``digital_input``) keeps driving capture
# after an upgrade even though it now classifies as ``detection``: the point of
# this revision is that an operator-wired input is not noise. The list is pinned
# by ``test_beta7_backfilled_selector_keeps_security_events`` and must not widen.
LEGACY_FILTER_CLASSES: frozenset[str] = frozenset(
    {EVENT_CLASS_MOTION, EVENT_CLASS_HEARTBEAT, EVENT_CLASS_CONDITION}
)
LEGACY_FILTER_SELECTOR_JSON = json.dumps(sorted(LEGACY_FILTER_CLASSES))


def normalize_device_selector(value: object) -> list[str] | None:
    """Normalize a Device-level selector, preserving the ``inherit`` sentinel.

    Returns ``None`` for inherit (unset, ``None``, or the literal ``inherit``
    used by the storage column) so the explicit-negative case stays distinct, and
    a sorted class list otherwise.

    A value that cannot be read as a selector at all (corrupt storage, a wrong
    type) also returns ``None``: a damaged row is not an operator statement, so
    it must not become an invented "this camera filters nothing" choice that
    quietly overrides the active Capture Profile forever. Falling back to inherit
    keeps profile policy authoritative and heals when the row is next saved.
    Operator input is validated at the API edge, so only corruption lands here.
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text.startswith("["):
            return None
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            return None
    if not isinstance(value, (list, tuple, set, frozenset)):
        return None
    return sorted(normalize_selector(value))


def encode_device_filter(value: list[str] | None) -> str:
    """Encode a Device selector for storage, mapping inherit to the sentinel."""
    if value is None:
        return INHERIT_EVENT_FILTER
    return json.dumps(sorted(value), separators=(",", ":"))


def decode_device_filter(value: object) -> list[str] | None:
    """Decode a stored Device selector, mapping the sentinel back to inherit."""
    if isinstance(value, str) and value.strip() == INHERIT_EVENT_FILTER:
        return None
    return normalize_device_selector(value)


def event_class(event_type: str | None) -> str:
    """Return the class for a normalized event type (``unknown`` if unrecognized)."""
    if not event_type:
        return EVENT_CLASS_UNKNOWN
    return EVENT_CLASS_BY_TYPE.get(str(event_type), EVENT_CLASS_UNKNOWN)


def selectable_event_classes(level: str) -> frozenset[str]:
    """Return the classes a level may offer to an operator."""
    if level == "profile":
        return PROFILE_SELECTABLE_EVENT_CLASSES
    if level == "device":
        return DEVICE_SELECTABLE_EVENT_CLASSES
    raise ValueError(f"Unknown event filter level: {level}")


def _decoded_selector(value: object) -> object:
    """Decode the stored-string form of a selector, including legacy shapes."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if text.startswith("["):
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            return None
    return None


def normalize_selector(value: object) -> frozenset[str]:
    """Return the selected classes, always as a set of filterable class names.

    An unset, malformed, or unselectable value yields an empty set, so a bad
    value read back from storage means "no filtering" and can never widen
    filtering. The legacy ``beta.7`` shapes (a boolean, or the Device tri-state
    string) translate to the equivalent class set.
    """
    if value is None:
        return frozenset()
    if isinstance(value, bool):
        return frozenset(LEGACY_FILTER_CLASSES) if value else frozenset()
    if isinstance(value, str):
        text = value.strip()
        if text in ("enabled",):
            return frozenset(LEGACY_FILTER_CLASSES)
        decoded = _decoded_selector(text)
        if decoded is None:
            return frozenset()
        value = decoded
    if not isinstance(value, (list, tuple, set, frozenset)):
        return frozenset()
    names = {str(item).strip() for item in value}
    return frozenset(name for name in names if name in FILTERABLE_EVENT_CLASSES)


def validate_selector(value: object, *, level: str = "device") -> list[str]:
    """Validate an operator selector for a level, returning a canonical list.

    Raises ``ValueError`` naming the offending entry when a class is unknown,
    protected, duplicated, or not selectable at that level. Used at API and
    service boundaries; the ingestion decision path uses ``normalize_selector``
    so that damaged stored data fails safe instead of raising mid-delivery.
    """
    if value is None:
        return []
    if isinstance(value, bool):
        return sorted(LEGACY_FILTER_CLASSES) if value else []
    if isinstance(value, str):
        text = value.strip()
        if text in ("", "inherit", "disabled"):
            return []
        if text == "enabled":
            return sorted(LEGACY_FILTER_CLASSES)
        decoded = _decoded_selector(text)
        if decoded is None:
            raise ValueError(f"Unknown event class: {text}")
        return validate_selector(decoded, level=level)
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise ValueError("Event filter must be a list of event class names")

    allowed = selectable_event_classes(level)
    seen: list[str] = []
    for raw in value:
        name = str(raw).strip()
        if name not in EVENT_CLASSES:
            raise ValueError(f"Unknown event class: {name}")
        if name not in allowed:
            raise ValueError(f"Event class {name} cannot be filtered at {level} level")
        if name in seen:
            raise ValueError(f"Event class {name} is selected more than once")
        seen.append(name)
    return sorted(seen)


def is_filterable(event_type: str | None, selector: frozenset[str] | set[str]) -> bool:
    """Return whether an event type is suppressed by a selector.

    False only for an empty selector or a class the selector does not name.
    Suppression stops an Event driving capture; it never discards the Raw
    Artifact, Receipt, or canonical Event.
    """
    if not selector:
        return False
    return event_class(event_type) in selector
