"""Unit tests for the Reolink snapshot pre-arm slot (Phase 1, gap G2)."""

from __future__ import annotations

import asyncio

import pytest

from episode.plugins.reolink.snapshot_slot import (
    SnapshotPrearmSettings,
    SnapshotSlot,
    parse_prearm_settings,
)


class FakeClock:
    """Monotonic clock the test advances explicitly."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.mark.asyncio
async def test_slot_serves_one_fetch_then_misses():
    calls = []

    async def fetch():
        calls.append(1)
        return b"\xff\xd8jpeg", "image/jpeg"

    clock = FakeClock()
    slot = SnapshotSlot(fetch, settings=SnapshotPrearmSettings(ttl=2.0), clock=clock)

    assert slot.consume() is None  # nothing armed yet
    assert slot.arm() is True
    await asyncio.sleep(0)  # let the task run
    assert len(calls) == 1

    served = slot.consume()
    assert served == (b"\xff\xd8jpeg", "image/jpeg")
    assert slot.consume() is None  # consumed once
    assert slot.counters == {
        "armed": 1,
        "hits": 1,
        "misses": 2,
        "expired": 0,
        "failed": 0,
    }


@pytest.mark.asyncio
async def test_slot_expired_snapshot_is_not_served():
    async def fetch():
        return b"\xff\xd8jpeg", "image/jpeg"

    clock = FakeClock()
    slot = SnapshotSlot(fetch, settings=SnapshotPrearmSettings(ttl=2.0), clock=clock)
    slot.arm()
    await asyncio.sleep(0)

    clock.advance(2.5)
    assert slot.consume() is None
    assert slot.counters["expired"] == 1


@pytest.mark.asyncio
async def test_slot_respects_min_interval():
    calls = []

    async def fetch():
        calls.append(1)
        return b"\xff\xd8jpeg", "image/jpeg"

    clock = FakeClock()
    slot = SnapshotSlot(
        fetch, settings=SnapshotPrearmSettings(ttl=2.0, min_interval=30.0), clock=clock
    )
    assert slot.arm() is True
    await asyncio.sleep(0)

    assert slot.arm() is False  # already in flight
    clock.advance(1.0)
    assert slot.arm() is False  # inside min_interval
    clock.advance(60.0)
    assert slot.arm() is True
    await asyncio.sleep(0)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_slot_disabled_never_arms():
    calls = []

    async def fetch():
        calls.append(1)
        return b"\xff\xd8jpeg", "image/jpeg"

    slot = SnapshotSlot(fetch, settings=SnapshotPrearmSettings(enabled=False))
    assert slot.enabled is False
    assert slot.arm() is False
    await asyncio.sleep(0)
    assert calls == []


@pytest.mark.asyncio
async def test_slot_failure_is_counted_and_not_raised():
    async def fetch():
        raise RuntimeError("camera busy")

    slot = SnapshotSlot(fetch)
    assert slot.arm() is True
    await asyncio.sleep(0)
    assert slot.counters["failed"] == 1
    assert slot.consume() is None
    # A failed pre-arm must leave the slot free to arm again.
    assert slot.counters["armed"] == 1


@pytest.mark.asyncio
async def test_slot_cancel_stops_inflight_fetch():
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def fetch():
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return b"\xff\xd8", "image/jpeg"

    slot = SnapshotSlot(fetch)
    slot.arm()
    await started.wait()
    await slot.cancel()
    assert cancelled.is_set()
    assert slot._task is None


def test_parse_prearm_settings_defaults():
    settings, warnings = parse_prearm_settings({})
    assert warnings == []
    assert settings == SnapshotPrearmSettings(enabled=True, ttl=2.0, min_interval=1.0)


def test_parse_prearm_settings_rejects_out_of_range_with_warning():
    settings, warnings = parse_prearm_settings(
        {"snapshot_prearm_ttl": 99.0, "snapshot_prearm_min_interval": "abc"}
    )
    assert settings.ttl == 2.0
    assert settings.min_interval == 1.0
    assert len(warnings) == 2


def test_parse_prearm_settings_accepts_valid_values():
    settings, warnings = parse_prearm_settings(
        {
            "snapshot_prearm": False,
            "snapshot_prearm_ttl": 5,
            "snapshot_prearm_min_interval": 0.0,
        }
    )
    assert warnings == []
    assert settings == SnapshotPrearmSettings(enabled=False, ttl=5.0, min_interval=0.0)


@pytest.mark.asyncio
async def test_parse_prearm_settings_rejects_non_boolean_enabled():
    settings, warnings = parse_prearm_settings({"snapshot_prearm": "yes"})
    assert settings.enabled is True
    assert warnings


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
