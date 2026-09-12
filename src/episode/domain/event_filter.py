from __future__ import annotations

"""Generic event classification for the capture-participation filter.

The core classifies canonical, vendor-neutral ``event_type`` strings to decide
whether an observation is "generic" (low-signal: motion, system, video loss,
tamper, audio) or a higher-level detection (person, vehicle, pet, etc.). This
is pure domain logic and must not import plugins or vendor code.

Unknown event types are treated as high-level (not filtered) so uncertain or
unrecognized messages remain preserved and unclaimed, matching the project's
"unknown messages remain preserved" rule.
"""

GENERIC_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "motion_detection",
        "video_loss",
        "tamper_detection",
        "tampering_detection",
        "audio_detection",
        "digital_input",
        "system",
        "battery_low",
        "battery_status",
    }
)

# High-level detections are never filtered because they already represent a
# classified target. Kept explicit for documentation and future validation.
HIGH_LEVEL_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "human_detection",
        "vehicle_detection",
        "pet_detection",
        "animal_detection",
        "face_detection",
        "line_crossing_detection",
        "loitering_detection",
        "ladder_detection",
        "package_detection",
        "doorbell",
        "door_access",
        "card",
    }
)

# The union of generic and high-level sets is the recognized canonical set.
RECOGNIZED_EVENT_TYPES: frozenset[str] = GENERIC_EVENT_TYPES | HIGH_LEVEL_EVENT_TYPES


def is_generic_event_type(event_type: str) -> bool:
    """Return whether a normalized event type is generic (subject to filtering).

    Only the canonical generic set is considered generic. High-level detections
    and unknown types are treated as high-signal and are not filtered.
    """
    return event_type in GENERIC_EVENT_TYPES
