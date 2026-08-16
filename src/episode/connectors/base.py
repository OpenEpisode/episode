from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Protocol

logger = logging.getLogger(__name__)


class ManagedConnector(Protocol):
    name: str

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def status(self) -> dict: ...


class Connector(ABC):
    def __init__(self, name: str, config: dict):
        self.name = name
        self._config = config
        self._running = False

    @abstractmethod
    async def start(self): ...

    async def stop(self):
        self._running = False

    def status(self) -> dict:
        return {
            "name": self.name,
            "type": self.__class__.__name__.removesuffix("Connector").lower(),
            "running": self._running,
        }
