"""Shared parsing helper for Reolink device settings.

Every Reolink setting has to survive the same failure modes — a missing key, a wrong
type, a number outside its useful range — and none of them may stop a device from
connecting. Keeping the one implementation here means a warning reads the same way
however the setting was reached, and a device never gets a different second chance
because its setting landed in a different module.
"""

from __future__ import annotations

from typing import Any


def bounded_float(
    settings: dict[str, Any],
    name: str,
    default: float,
    bounds: tuple[float, float],
    warnings: list[str],
) -> float:
    """One bounded numeric setting from ``settings``; the default plus a warning otherwise.

    Startup must not fail on a bad value: the device still connects, and the operator sees
    a warning naming the key rather than a dead integration. A missing key is not a
    warning — the default is simply what was not asked about.
    """
    value = settings.get(name)
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        warnings.append(f"{name} must be a number; using default")
        return default
    low, high = bounds
    if not low <= parsed <= high:
        warnings.append(f"{name} must be between {low} and {high}; using default")
        return default
    return parsed


def strict_bool(
    settings: dict[str, Any],
    name: str,
    default: bool,
    warnings: list[str],
) -> bool:
    """One boolean setting from ``settings``; the default plus a warning otherwise.

    Deliberately stricter than ``bool(value)``: ``"false"`` is truthy in Python, so an
    operator writing that in a config would otherwise get the opposite of what they asked
    for. A string is accepted only when it actually says so.
    """
    value = settings.get(name)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1", "on"}:
            return True
        if lowered in {"false", "no", "0", "off"}:
            return False
    warnings.append(f"{name} must be a boolean; using default")
    return default
