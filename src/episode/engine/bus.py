from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

EventHandler = Callable[..., Coroutine[Any, Any, None]]


@dataclass
class Message:
    type: str
    data: dict = field(default_factory=dict)


class EventBus:
    def __init__(self):
        self._subscribers: dict[str, list[EventHandler]] = {}

    def subscribe(self, message_type: str, handler: EventHandler):
        self._subscribers.setdefault(message_type, []).append(handler)
        logger.debug("Subscribed %s to %s", handler.__name__, message_type)

    def unsubscribe(self, message_type: str, handler: EventHandler):
        handlers = self._subscribers.get(message_type, [])
        if handler in handlers:
            handlers.remove(handler)

    async def publish(self, message: Message):
        msg_type = message.type
        handlers = self._subscribers.get(msg_type, [])
        general = self._subscribers.get("*", [])
        for handler in [*handlers, *general]:
            try:
                await handler(message)
            except Exception:
                logger.exception("Handler %s failed for message %s", handler.__name__, msg_type)
