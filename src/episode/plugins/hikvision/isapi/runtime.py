from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from urllib.parse import urlunsplit

import httpx

from episode.plugins.hikvision.xml_events import HikvisionEvent
from episode.plugins.models import (
    PluginInstanceState,
    PluginInstanceStatus,
    RawPluginDelivery,
    RawPluginDeliverySink,
)

logger = logging.getLogger(__name__)

DEFAULT_EVENT_PATH = "/ISAPI/Event/notification/alertStream"
DEFAULT_MAX_BUFFER_BYTES = 4 * 1024 * 1024
_XML_END = b"</EventNotificationAlert>"
_XML_STARTS = (b"<?xml", b"<EventNotificationAlert")


@dataclass(frozen=True)
class ISAPIDeviceConfig:
    id: str
    name: str
    area_id: str
    address: str
    username: str
    password: str
    protocol: str = "http"
    port: int = 80
    path: str = DEFAULT_EVENT_PATH
    ignore_events: tuple[str, ...] = ()

    @property
    def url(self) -> str:
        default_port = 443 if self.protocol == "https" else 80
        port = f":{self.port}" if self.port != default_port else ""
        return urlunsplit((self.protocol, f"{self.address}{port}", self.path, "", ""))


class ISAPIEventStreamDecoder:
    """Extract complete EventNotificationAlert documents from arbitrary chunks."""

    def __init__(self, max_buffer_bytes: int = DEFAULT_MAX_BUFFER_BYTES):
        self._buffer = bytearray()
        self._max_buffer_bytes = max_buffer_bytes

    def feed(self, chunk: bytes) -> tuple[bytes, ...]:
        self._buffer.extend(chunk)
        documents: list[bytes] = []
        while True:
            starts = [
                offset for marker in _XML_STARTS if (offset := self._buffer.find(marker)) >= 0
            ]
            if not starts:
                self._retain_possible_start()
                break
            start = min(starts)
            if start:
                del self._buffer[:start]
            end = self._buffer.find(_XML_END)
            if end < 0:
                break
            end += len(_XML_END)
            documents.append(bytes(self._buffer[:end]))
            del self._buffer[:end]

        if len(self._buffer) > self._max_buffer_bytes:
            self._buffer.clear()
            raise ValueError("ISAPI event stream exceeded the bounded receive buffer")
        return tuple(documents)

    def _retain_possible_start(self) -> None:
        keep = max(len(marker) for marker in _XML_STARTS) - 1
        if len(self._buffer) > keep:
            del self._buffer[:-keep]


ClientFactory = Callable[[httpx.DigestAuth], httpx.AsyncClient]


def _default_client_factory(auth: httpx.DigestAuth) -> httpx.AsyncClient:
    return httpx.AsyncClient(auth=auth, timeout=None, follow_redirects=False)


class ISAPIDeviceConnection:
    """Own one reconnecting ISAPI stream without leaking vendor logic into core."""

    def __init__(
        self,
        config: ISAPIDeviceConfig,
        delivery_sink: RawPluginDeliverySink,
        *,
        client_factory: ClientFactory = _default_client_factory,
        reconnect_delay: float = 5.0,
        max_buffer_bytes: int = DEFAULT_MAX_BUFFER_BYTES,
    ):
        self.config = config
        self._delivery_sink = delivery_sink
        self._client_factory = client_factory
        self._reconnect_delay = reconnect_delay
        self._max_buffer_bytes = max_buffer_bytes
        self._client: httpx.AsyncClient | None = None
        self._task: asyncio.Task | None = None
        self._running = False
        self._ignored_states: dict[tuple[str, str], str] = {}
        self._status = PluginInstanceStatus(
            id=config.id,
            name=config.name,
            state=PluginInstanceState.STARTING,
        )

    def status(self) -> PluginInstanceStatus:
        return self._status

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._client = self._client_factory(
            httpx.DigestAuth(self.config.username, self.config.password)
        )
        self._task = asyncio.create_task(
            self._run(),
            name=f"hikvision-isapi:{self.config.id}",
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._client:
            await self._client.aclose()
            self._client = None
        self._status = replace(self._status, state=PluginInstanceState.STOPPED)

    async def _run(self) -> None:
        assert self._client is not None
        while self._running:
            decoder = ISAPIEventStreamDecoder(self._max_buffer_bytes)
            try:
                async with self._client.stream("GET", self.config.url) as response:
                    response.raise_for_status()
                    self._status = replace(
                        self._status,
                        state=PluginInstanceState.RUNNING,
                        connected_at=datetime.now(tz=timezone.utc),
                        error=None,
                    )
                    logger.info(
                        "Hikvision ISAPI connected for device %s (%s)",
                        self.config.id,
                        self.config.url,
                    )
                    async for chunk in response.aiter_bytes():
                        for payload in decoder.feed(chunk):
                            await self._preserve(payload)
                if self._running:
                    self._set_reconnecting("ISAPI event stream ended; reconnecting.")
            except asyncio.CancelledError:
                raise
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                error = f"ISAPI returned HTTP {status_code}."
                if status_code in {401, 403}:
                    self._set_failed(error)
                else:
                    self._set_reconnecting(error)
            except (httpx.RequestError, ValueError) as exc:
                self._set_reconnecting(f"ISAPI connection failed ({exc.__class__.__name__}).")
            except Exception:
                logger.exception("Unexpected ISAPI failure for device %s", self.config.id)
                self._set_reconnecting("ISAPI stream failed unexpectedly.")

            if self._running:
                await asyncio.sleep(self._reconnect_delay)

    async def _preserve(self, payload: bytes) -> None:
        received_at = datetime.now(tz=timezone.utc)
        if self._is_repeated_ignored_state(payload):
            self._record_delivery(received_at, suppressed=True)
            return
        await self._delivery_sink(
            RawPluginDelivery(
                plugin_id="hikvision-isapi",
                device_id=self.config.id,
                area_id=self.config.area_id,
                received_at=received_at,
                payload=payload,
                source="hikvision:isapi",
                media_type="application/xml",
                artifact_type="event_payload",
                metadata={
                    "integration": "isapi",
                    "ignore_events": list(self.config.ignore_events),
                },
            )
        )
        self._record_delivery(received_at, suppressed=False)

    def _is_repeated_ignored_state(self, payload: bytes) -> bool:
        parsed = HikvisionEvent.from_bytes(payload)
        if parsed is None:
            return False
        ignored = {name.strip().lower() for name in self.config.ignore_events}
        if not {parsed.event_type.lower(), parsed.vendor_event_type.lower()} & ignored:
            return False
        key = (parsed.vendor_event_type.lower(), parsed.channel_name.lower())
        state = parsed.event_state.value
        repeated = self._ignored_states.get(key) == state
        self._ignored_states[key] = state
        return repeated

    def _record_delivery(self, received_at: datetime, *, suppressed: bool) -> None:
        details = dict(self._status.details)
        metric = "deliveries_suppressed" if suppressed else "deliveries_preserved"
        details[metric] = int(details.get(metric, 0)) + 1
        self._status = replace(
            self._status,
            messages_received=self._status.messages_received + 1,
            last_message_at=received_at,
            error=None,
            details=details,
        )

    def _set_reconnecting(self, error: str) -> None:
        logger.warning("Hikvision ISAPI device %s: %s", self.config.id, error)
        self._status = replace(
            self._status,
            state=PluginInstanceState.STARTING,
            error=error,
        )

    def _set_failed(self, error: str) -> None:
        logger.warning("Hikvision ISAPI device %s: %s", self.config.id, error)
        self._status = replace(
            self._status,
            state=PluginInstanceState.FAILED,
            error=error,
        )


def device_config(value: Mapping[str, object]) -> tuple[ISAPIDeviceConfig | None, str | None]:
    device_id = value.get("id")
    name = value.get("name")
    area_id = value.get("area_id")
    address = value.get("ip_address")
    username = value.get("username")
    password = value.get("password")
    configs = value.get("configs", {})
    isapi = configs.get("isapi", {}) if isinstance(configs, Mapping) else {}
    settings = isapi.get("settings", {}) if isinstance(isapi, Mapping) else {}
    protocol = isapi.get("protocol", "http") if isinstance(isapi, Mapping) else "http"
    port = isapi.get("port", 80) if isinstance(isapi, Mapping) else 80
    path = (
        isapi.get("path", DEFAULT_EVENT_PATH) if isinstance(isapi, Mapping) else DEFAULT_EVENT_PATH
    )
    ignore_events = settings.get("ignore_events", ()) if isinstance(settings, Mapping) else ()

    required = {
        "id": device_id,
        "name": name,
        "area_id": area_id,
        "ip_address": address,
        "username": username,
        "password": password,
    }
    missing = [key for key, item in required.items() if not isinstance(item, str) or not item]
    if missing:
        return None, f"Missing ISAPI device configuration: {', '.join(missing)}."
    if protocol not in {"http", "https"}:
        return None, "ISAPI protocol must be http or https."
    if not isinstance(port, int) or not 1 <= port <= 65535:
        return None, "ISAPI port must be between 1 and 65535."
    if not isinstance(path, str) or not path.startswith("/"):
        return None, "ISAPI path must begin with /."
    if not isinstance(ignore_events, (list, tuple)) or not all(
        isinstance(item, str) for item in ignore_events
    ):
        return None, "ISAPI ignored Events must be a list of names."

    return (
        ISAPIDeviceConfig(
            id=device_id,
            name=name,
            area_id=area_id,
            address=address,
            username=username,
            password=password,
            protocol=protocol,
            port=port,
            path=path,
            ignore_events=tuple(ignore_events),
        ),
        None,
    )
