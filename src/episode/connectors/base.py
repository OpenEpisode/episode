from __future__ import annotations

from typing import Protocol


class ManagedConnector(Protocol):
    name: str

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def status(self) -> dict: ...
