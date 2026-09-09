"""Vendor-neutral Episode lifecycle policy values."""

from __future__ import annotations

DEFAULT_QUIESCENT_GRACE_SECONDS = 5
MIN_QUIESCENT_GRACE_SECONDS = 0
MAX_QUIESCENT_GRACE_SECONDS = 60
QUIESCENT_GRACE_SETTING = "episode_quiescent_grace_seconds"


def validate_quiescent_grace_seconds(value: int) -> int:
    """Validate and normalize the short post-deadline settling window."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("quiescent grace must be an integer")
    if not MIN_QUIESCENT_GRACE_SECONDS <= value <= MAX_QUIESCENT_GRACE_SECONDS:
        raise ValueError(
            "quiescent grace must be between "
            f"{MIN_QUIESCENT_GRACE_SECONDS} and {MAX_QUIESCENT_GRACE_SECONDS} seconds"
        )
    return value
