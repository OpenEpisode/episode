"""Dependency-free example of an out-of-tree Episode Device plugin."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

from episode.plugin_api import (
    EventObservation,
    HandlerRegistration,
    HandlerResult,
    PluginConfigurationError,
    PluginContext,
    PluginState,
    PluginStatus,
    RawDelivery,
    ReceiptStatus,
)

logger = logging.getLogger("episode.external.example_udp_sensor")
_EVENT_TYPE = re.compile(rb"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")


class _Datagrams(asyncio.DatagramProtocol):
    def __init__(self, plugin: "UDPSensorPlugin") -> None:
        self._plugin = plugin

    def datagram_received(self, data: bytes, address) -> None:
        self._plugin.receive(data, address)

    def error_received(self, error: Exception) -> None:
        self._plugin.report_error(error)


class UDPSensorPlugin:
    def __init__(self, context: PluginContext) -> None:
        if len(context.devices) != 1:
            raise PluginConfigurationError("The UDP sensor example requires exactly one Device.")
        self._context = context
        self._device = context.devices[0]
        self._host = str(context.settings.get("host", "0.0.0.0"))
        self._port = int(context.settings.get("port", 9876))
        if not 0 <= self._port <= 65535:
            raise PluginConfigurationError("UDP port must be between 1 and 65535.")
        self._transport: asyncio.DatagramTransport | None = None
        self._tasks: set[asyncio.Task] = set()
        self._messages = 0
        self._last_message_at: datetime | None = None
        self._last_error: str | None = None

    @staticmethod
    def _matches(delivery) -> bool:
        return delivery.media_type == "text/plain"

    @staticmethod
    async def _handle(delivery) -> HandlerResult:
        event_type = delivery.payload.strip()
        event_state = b"active"
        for suffix, state in ((b":inactive", b"inactive"), (b":active", b"active")):
            if event_type.endswith(suffix):
                event_type = event_type[: -len(suffix)]
                event_state = state
                break
        if _EVENT_TYPE.fullmatch(event_type) is None:
            return HandlerResult(
                claimed=True,
                status=ReceiptStatus.REJECTED,
                metadata={"reason": "expected_event-type_colon_state"},
            )
        return HandlerResult(
            claimed=True,
            event=EventObservation(
                timestamp=delivery.received_at,
                event_type=event_type.decode("ascii"),
                event_state=event_state.decode("ascii"),
                source="example:udp",
                metadata={"transport": "udp"},
            ),
        )

    def receive(self, payload: bytes, address) -> None:
        self._messages += 1
        self._last_message_at = datetime.now(tz=timezone.utc)
        task = asyncio.create_task(
            self._context.ingress.submit(
                RawDelivery(
                    device_id=self._device.id,
                    received_at=self._last_message_at,
                    payload=payload,
                    source="example:udp",
                    media_type="text/plain",
                    metadata={"remote_address": list(address)},
                )
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._delivery_finished)

    def _delivery_finished(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as error:
            self.report_error(error)
            logger.exception("UDP delivery failed")

    def report_error(self, error: Exception) -> None:
        self._last_error = f"{error.__class__.__name__}: {error}"

    def status(self) -> PluginStatus:
        ingress = self._context.ingress.status("events") or {}
        address = self._transport.get_extra_info("sockname") if self._transport else None
        return PluginStatus(
            state=(
                PluginState.DEGRADED
                if self._last_error
                else PluginState.READY
                if self._transport
                else PluginState.FAILED
            ),
            error=self._last_error,
            summary=(f"Listening on UDP {address[0]}:{address[1]}" if address else "Not running"),
            metrics={
                "messages_received": self._messages,
                "last_message_at": self._last_message_at,
                "listen_address": list(address) if address else None,
                "handler": ingress,
            },
        )

    async def start(self) -> None:
        self._context.ingress.register(
            HandlerRegistration(id="events", matcher=self._matches, handler=self._handle)
        )
        loop = asyncio.get_running_loop()
        transport, _protocol = await loop.create_datagram_endpoint(
            lambda: _Datagrams(self),
            local_addr=(self._host, self._port),
        )
        self._transport = transport

    async def stop(self) -> None:
        if self._transport:
            self._transport.close()
            self._transport = None
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._context.ingress.unregister("events")


def create_plugin(context: PluginContext) -> UDPSensorPlugin:
    return UDPSensorPlugin(context)
