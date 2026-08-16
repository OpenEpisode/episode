from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

AsyncCallback = Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class _Cleanup:
    name: str
    callback: AsyncCallback


class Lifecycle:
    """Track entered resources and unwind them in reverse order."""

    def __init__(self) -> None:
        self._cleanups: list[_Cleanup] = []
        self._shutdown_lock = asyncio.Lock()
        self._closed = False

    async def start(
        self,
        name: str,
        start: AsyncCallback,
        stop: AsyncCallback,
    ) -> None:
        if self._closed:
            raise RuntimeError("Application lifecycle is already closed")
        # Register first so a partially completed start is still cleaned up.
        self._cleanups.append(_Cleanup(name, stop))
        await start()

    async def shutdown(self) -> None:
        async with self._shutdown_lock:
            if self._closed:
                return
            self._closed = True
            cleanups = list(reversed(self._cleanups))
            self._cleanups.clear()
            cancellation: asyncio.CancelledError | None = None
            for cleanup in cleanups:
                try:
                    await cleanup.callback()
                except asyncio.CancelledError as error:
                    cancellation = cancellation or error
                    logger.warning("%s was cancelled during shutdown", cleanup.name)
                except Exception:
                    logger.exception("%s failed during shutdown", cleanup.name)
            if cancellation:
                raise cancellation
