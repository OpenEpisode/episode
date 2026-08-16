from __future__ import annotations

import asyncio

import pytest

from episode.lifecycle import Lifecycle


@pytest.mark.asyncio
async def test_shutdown_is_reverse_order_best_effort_and_idempotent():
    events: list[str] = []
    lifecycle = Lifecycle()

    async def start(name: str) -> None:
        events.append(f"start:{name}")

    async def stop(name: str, *, fails: bool = False) -> None:
        events.append(f"stop:{name}")
        if fails:
            raise RuntimeError(f"{name} failed")

    await lifecycle.start(
        "storage",
        lambda: start("storage"),
        lambda: stop("storage"),
    )
    await lifecycle.start(
        "plugins",
        lambda: start("plugins"),
        lambda: stop("plugins", fails=True),
    )
    await lifecycle.start(
        "connector",
        lambda: start("connector"),
        lambda: stop("connector"),
    )

    await lifecycle.shutdown()
    await lifecycle.shutdown()

    assert events == [
        "start:storage",
        "start:plugins",
        "start:connector",
        "stop:connector",
        "stop:plugins",
        "stop:storage",
    ]


@pytest.mark.asyncio
async def test_partially_started_resource_is_still_cleaned_up():
    events: list[str] = []
    lifecycle = Lifecycle()

    async def broken_start() -> None:
        events.append("start")
        raise RuntimeError("startup failed")

    async def stop() -> None:
        events.append("stop")

    with pytest.raises(RuntimeError, match="startup failed"):
        await lifecycle.start("partial", broken_start, stop)

    await lifecycle.shutdown()

    assert events == ["start", "stop"]


@pytest.mark.asyncio
async def test_cancellation_does_not_skip_remaining_cleanup():
    events: list[str] = []
    lifecycle = Lifecycle()

    async def start() -> None:
        pass

    async def stop(name: str, *, cancelled: bool = False) -> None:
        events.append(name)
        if cancelled:
            raise asyncio.CancelledError

    await lifecycle.start("storage", start, lambda: stop("storage"))
    await lifecycle.start(
        "plugin",
        start,
        lambda: stop("plugin", cancelled=True),
    )
    await lifecycle.start("connector", start, lambda: stop("connector"))

    with pytest.raises(asyncio.CancelledError):
        await lifecycle.shutdown()

    assert events == ["connector", "plugin", "storage"]
