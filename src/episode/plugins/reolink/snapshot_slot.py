"""Pre-armed snapshot slot: overlap camera JPEG encode with event processing.

A Reolink snapshot (``cmdId=109``) costs 0.3–1.1 s of camera encode time. Pulling
it only after the event is canonical makes that cost fully serial with event
processing. The slot lets the device arm a fetch as soon as the event frame
arrives, then serve the result to the ordinary snapshot fetcher if it is still
fresh.

The slot never produces a delivery and never becomes Evidence, so the raw-first
boundary is untouched: it is an optimisation over an already-preserved frame.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

SnapshotFetch = Callable[[], Awaitable[tuple[bytes, str]]]

SNAPSHOT_PREARM_DEFAULT = True
SNAPSHOT_PREARM_TTL_DEFAULT = 2.0
SNAPSHOT_PREARM_TTL_BOUNDS = (0.5, 10.0)
SNAPSHOT_PREARM_MIN_INTERVAL_DEFAULT = 1.0
SNAPSHOT_PREARM_MIN_INTERVAL_BOUNDS = (0.0, 60.0)


@dataclass(frozen=True)
class SnapshotPrearmSettings:
    """Validated pre-arm configuration for one device."""

    enabled: bool = SNAPSHOT_PREARM_DEFAULT
    ttl: float = SNAPSHOT_PREARM_TTL_DEFAULT
    min_interval: float = SNAPSHOT_PREARM_MIN_INTERVAL_DEFAULT


def parse_prearm_settings(
    settings: dict[str, object],
) -> tuple[SnapshotPrearmSettings, list[str]]:
    """Read pre-arm settings, falling back to defaults and reporting warnings.

    Never raises: a bad value must not stop a device from connecting.
    """
    warnings: list[str] = []
    enabled = settings.get("snapshot_prearm", SNAPSHOT_PREARM_DEFAULT)
    if not isinstance(enabled, bool):
        warnings.append("snapshot_prearm must be a boolean; using default")
        enabled = SNAPSHOT_PREARM_DEFAULT

    ttl = _bounded_float(
        settings.get("snapshot_prearm_ttl"),
        SNAPSHOT_PREARM_TTL_DEFAULT,
        SNAPSHOT_PREARM_TTL_BOUNDS,
        "snapshot_prearm_ttl",
        warnings,
    )
    min_interval = _bounded_float(
        settings.get("snapshot_prearm_min_interval"),
        SNAPSHOT_PREARM_MIN_INTERVAL_DEFAULT,
        SNAPSHOT_PREARM_MIN_INTERVAL_BOUNDS,
        "snapshot_prearm_min_interval",
        warnings,
    )
    return SnapshotPrearmSettings(bool(enabled), ttl, min_interval), warnings


def _bounded_float(
    value: object,
    default: float,
    bounds: tuple[float, float],
    name: str,
    warnings: list[str],
) -> float:
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


class SnapshotSlot:
    """Hold one pre-fetched snapshot so a later request can skip the encode."""

    def __init__(
        self,
        fetch: SnapshotFetch,
        *,
        settings: SnapshotPrearmSettings | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch
        self._settings = settings or SnapshotPrearmSettings()
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
        self._content: tuple[bytes, str, float] | None = None
        self._last_arm_at: float | None = None
        self.counters = {"armed": 0, "hits": 0, "misses": 0, "expired": 0, "failed": 0}

    @property
    def enabled(self) -> bool:
        """Whether pre-arming is active for this device."""
        return self._settings.enabled

    def arm(self) -> bool:
        """Start one background snapshot fetch; never raises into the caller.

        Returns True when a fetch was started. Fire-and-forget: a no-op while a
        fetch is in flight, and a no-op inside the ``min_interval`` guard.
        """
        if not self._settings.enabled or self._task is not None:
            return False
        now = self._clock()
        if self._last_arm_at is not None and now - self._last_arm_at < self._settings.min_interval:
            return False
        self._last_arm_at = now
        self.counters["armed"] += 1
        self._task = asyncio.create_task(self._run())
        return True

    async def _run(self) -> None:
        try:
            content, content_type = await self._fetch()
        except Exception as error:  # pre-arm failure must not escape
            self.counters["failed"] += 1
            logger.debug("Reolink snapshot pre-arm failed: %s", error)
            return
        finally:
            self._task = None
        self._content = (content, content_type, self._clock() + self._settings.ttl)

    def consume(self) -> tuple[bytes, str] | None:
        """Return a still-fresh pre-armed snapshot, or None (counting why not)."""
        held, self._content = self._content, None
        if held is None:
            self.counters["misses"] += 1
            return None
        content, content_type, deadline = held
        if self._clock() > deadline:
            self.counters["expired"] += 1
            return None
        self.counters["hits"] += 1
        return content, content_type

    async def cancel(self) -> None:
        """Cancel any in-flight fetch so no task outlives the connection."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._content = None
