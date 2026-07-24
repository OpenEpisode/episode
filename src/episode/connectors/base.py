from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from episode.engine.bus import EventBus

logger = logging.getLogger(__name__)


class Connector(ABC):
    def __init__(self, name: str, bus: EventBus, config: dict):
        self.name = name
        self._bus = bus
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
