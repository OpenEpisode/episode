"""Reolink device connection manager.

Handles the lifecycle of a Reolink camera connection including:
- TCP connection and binary protocol login
- Stream URL discovery
- Push-based event processing
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from episode.domain.models import CapabilityConfig, Device
from episode.media.registry import CameraMedia
from episode.plugins.models import (
    PluginDeviceUpdateSink,
    PluginInstanceState,
    PluginInstanceStatus,
    PluginMediaRegistry,
    RawPluginDelivery,
    RawPluginDeliverySink,
)
from episode.plugins.reolink.client import (
    BaichuanApiClient,
    ReolinkDeviceInfo,
    ReolinkError,
    ReolinkLoginError,
    ReolinkStreamError,
    StreamUrlInfo,
)
from episode.plugins.reolink.events import (
    ReolinkEvent,
    parse_alarm_event_frame,
    parse_battery_status_frame,
)

logger = logging.getLogger(__name__)

DEFAULT_API_PORT = 9000


@dataclass(frozen=True)
class ReolinkDeviceConfig:
    device: Device
    host: str = ""
    api_port: int = DEFAULT_API_PORT
    timeout: float = 10.0
    events_enabled: bool = False
    media_enabled: bool = False
    retry_delay: float = 30.0
    event_retry_delay: float = 5.0
    dedup_window: float = 5.0


ClientFactory = Callable[[ReolinkDeviceConfig], BaichuanApiClient]


def _default_client_factory(config: ReolinkDeviceConfig) -> BaichuanApiClient:
    """Create a BaichuanApiClient from a device config."""
    logger.debug(
        "Creating BaichuanApiClient: host=%s port=%d",
        config.host or config.device.ip_address,
        config.api_port,
    )
    return BaichuanApiClient(
        host=config.host or config.device.ip_address,
        username=config.device.username,
        password=config.device.password,
        api_port=config.api_port,
        timeout=config.timeout,
    )


class ReolinkDeviceConnection:
    """Per-device connection manager for Reolink Baichuan protocol."""

    def __init__(
        self,
        config: ReolinkDeviceConfig,
        delivery_sink: RawPluginDeliverySink,
        device_update_sink: PluginDeviceUpdateSink,
        *,
        media_registry: PluginMediaRegistry | None = None,
        client_factory: ClientFactory = _default_client_factory,
    ) -> None:
        """Initialize the device connection with its config and sinks."""
        self.config = config
        self._delivery_sink = delivery_sink
        self._device_update_sink = device_update_sink
        self._media_registry = media_registry
        self._media_registered = False
        self._client = client_factory(config)
        self._discovered: ReolinkDeviceInfo | None = None
        self._stream_url: StreamUrlInfo | None = None
        self._snapshot_supported: bool = False
        self._task: asyncio.Task | None = None
        self._event_task: asyncio.Task | None = None
        self._running = False
        self._status = PluginInstanceStatus(
            id=config.device.id,
            name=config.device.name,
            state=PluginInstanceState.STARTING,
        )
        logger.info(
            "ReolinkDeviceConnection created: device=%s id=%s events=%s",
            config.device.name,
            config.device.id,
            config.events_enabled,
        )

    def status(self) -> PluginInstanceStatus:
        """Return the current connection status."""
        return self._status

    async def start(self) -> None:
        """Authenticate and start the monitor and event listener tasks."""
        if self._running:
            logger.debug(
                "Reolink:%s already running, skipping start",
                self.config.device.name,
            )
            return
        self._running = True
        logger.info(
            "Reolink:%s starting device connection",
            self.config.device.name,
        )
        try:
            await self._authenticate()
        except Exception as error:
            self._set_error(error)
            logger.warning(
                "Reolink:%s initial login failed: %s",
                self.config.device.name,
                error,
            )

        # Start main monitor loop
        self._task = asyncio.create_task(
            self._monitor(),
            name=f"reolink:{self.config.device.id}",
        )

        # Start event listener if enabled
        if self.config.events_enabled:
            self._event_task = asyncio.create_task(
                self._event_loop(),
                name=f"reolink:{self.config.device.id}:events",
            )

        logger.info(
            "Reolink:%s monitor task started",
            self.config.device.name,
        )

    async def stop(self) -> None:
        """Cancel the event/monitor tasks and close the client connection."""
        logger.info(
            "Reolink:%s stopping device connection",
            self.config.device.name,
        )
        self._running = False

        # Cancel event task first
        if self._event_task:
            self._event_task.cancel()
            try:
                await self._event_task
            except asyncio.CancelledError:
                pass
            self._event_task = None

        # Cancel main task
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

        # Close client connection
        await self._client.close()
        await self._unregister_media()
        self._status = replace(self._status, state=PluginInstanceState.STOPPED)
        logger.info(
            "Reolink:%s stopped",
            self.config.device.name,
        )

    async def _authenticate(self) -> None:
        """Authenticate with the camera using Baichuan binary protocol."""
        logger.debug(
            "Reolink:%s authenticating with host=%s",
            self.config.device.name,
            self.config.host,
        )
        info = await self._client.login()
        self._discovered = info
        self._refresh_status()
        logger.info(
            "Reolink:%s authenticated · model=%s firmware=%s channels=%d",
            self.config.device.name,
            info.model or "unknown",
            info.firmware_version or "unknown",
            info.channel_count,
        )
        await self._discover_stream()

        # Subscribe to push events (cmdId=31) so the camera will actually send
        # alarm-event frames (cmdId=33). Reolink cameras stay silent until a
        # client explicitly subscribes, so without this no events ever arrive.
        if self.config.events_enabled:
            try:
                subscribed = await self._client.subscribe_events()
                if not subscribed:
                    logger.warning(
                        "Reolink:%s event subscription was rejected by the "
                        "camera; events may not be delivered",
                        self.config.device.name,
                    )
            except Exception as error:
                logger.warning(
                    "Reolink:%s event subscription failed: %s",
                    self.config.device.name,
                    error,
                )

    async def _discover_stream(self) -> None:
        """Discover stream URLs via StreamInfoList command."""
        logger.debug(
            "Reolink:%s discovering stream URL",
            self.config.device.name,
        )
        # Probe snapshot support first (best-effort) so it is known before
        # _apply_discovery persists device capabilities, ensuring the
        # "snapshots" capability is not lost (mirrors validation).
        try:
            snapshot = await self._client.get_snapshot(channel=0)
            self._snapshot_supported = bool(snapshot and snapshot[:2] == b"\xff\xd8")
        except Exception as error:
            logger.debug(
                "Reolink:%s snapshot probe failed: %s",
                self.config.device.name,
                error,
            )

        try:
            self._stream_url = await self._client.get_stream_url(channel=0)
            if self._stream_url and self._stream_url.success:
                logger.debug(
                    "Reolink:%s stream URL discovered: %s",
                    self.config.device.name,
                    self._stream_url.main_stream_url[:50]
                    if self._stream_url.main_stream_url
                    else "",
                )
                await self._apply_discovery()
        except Exception as error:
            logger.warning(
                "Reolink:%s stream discovery failed: %s",
                self.config.device.name,
                error,
            )
            self._set_error(error)

        # Register media endpoints (streams + snapshots) when enabled, so
        # recording and snapshot-on-event work for Reolink-only devices.
        if self.config.media_enabled:
            await self._register_media()

        # Refresh status now that stream and snapshot support are known, so
        # the runtime capabilities include media/snapshots (mirroring ONVIF,
        # which refreshes status after discovery).
        self._refresh_status()

    async def _apply_discovery(self) -> None:
        """Apply discovery results to device model."""
        logger.debug(
            "Reolink:%s applying discovery results",
            self.config.device.name,
        )
        device = self.config.device
        for capability in ("reolink",):
            if capability not in device.capabilities:
                device.capabilities.append(capability)
        if self._stream_url and self._stream_url.main_stream_url:
            if "media" not in device.capabilities:
                device.capabilities.append("media")
        if self._snapshot_supported and "snapshots" not in device.capabilities:
            device.capabilities.append("snapshots")
        if self._discovered:
            device.metadata["reolink"] = {
                "mac_address": self._discovered.mac_address,
                "model": self._discovered.model,
                "firmware_version": self._discovered.firmware_version,
                "channel_count": self._discovered.channel_count,
                "stream_url": self._stream_url.main_stream_url,
                "events_enabled": self.config.events_enabled,
                "media_enabled": self.config.media_enabled,
            }
        # Wire the discovered RTSP URL into the video config so the recording
        # engine can consume it for Reolink-only devices (preserving an
        # existing manual video config).
        if self.config.media_enabled and self._stream_url and self._stream_url.main_stream_url:
            existing_video = device.get_config("video")
            if existing_video and (existing_video.protocol or existing_video.path):
                recording_mode = existing_video.settings.get("recording_mode", "on_event")
                device.configs["video"] = CapabilityConfig(
                    protocol=existing_video.protocol,
                    port=existing_video.port,
                    path=existing_video.path,
                    settings={**existing_video.settings, "recording_mode": recording_mode},
                )
            else:
                device.configs["video"] = CapabilityConfig(
                    protocol="rtsp",
                    port=554,
                    path=self._stream_url.main_stream_url,
                    settings={"recording_mode": "on_event", "origin": "reolink"},
                )
        await self._device_update_sink(device)
        logger.debug(
            "Reolink:%s device update sent to sink",
            self.config.device.name,
        )

    async def _snapshot_fetcher(self) -> tuple[bytes, str]:
        """Reolink-native snapshot fetcher used by the media registry.

        Snapshots are fetched over the Baichuan binary protocol (cmdId=109),
        not HTTP, so this bypasses the registry's HTTP fetch path.
        """
        jpeg = await self._client.get_snapshot(channel=0)
        if not jpeg or jpeg[:2] != b"\xff\xd8":
            raise LookupError(f"Reolink snapshot unavailable for device {self.config.device.id}")
        return jpeg, "image/jpeg"

    async def _register_media(self) -> None:
        """Register a CameraMedia source (streams + snapshots) when enabled."""
        if self._media_registry is None:
            logger.warning(
                "Reolink:%s media enabled but runtime media registry unavailable",
                self.config.device.name,
            )
            return
        stream_uri = (
            self._stream_url.main_stream_url
            if self._stream_url and self._stream_url.main_stream_url
            else ""
        )
        if not stream_uri:
            logger.debug(
                "Reolink:%s no stream URL to register; skipping media registration",
                self.config.device.name,
            )
            return
        try:
            self._media_registry.register(
                CameraMedia(
                    device_id=self.config.device.id,
                    stream_uri=stream_uri,
                    username=self.config.device.username,
                    password=self.config.device.password,
                    profile_token="",
                    source="reolink",
                    snapshot_fetcher=self._snapshot_fetcher,
                )
            )
            self._media_registered = True
            logger.info(
                "Reolink:%s registered media source: stream=%s",
                self.config.device.name,
                stream_uri[:50],
            )
        except Exception as error:
            logger.warning(
                "Reolink:%s media registration failed: %s",
                self.config.device.name,
                error,
            )

    async def _unregister_media(self) -> None:
        """Unregister the media source for this device, if registered."""
        if self._media_registered and self._media_registry is not None:
            self._media_registry.unregister(self.config.device.id, source="reolink")
            self._media_registered = False
            logger.info(
                "Reolink:%s unregistered media source",
                self.config.device.name,
            )

    async def _monitor(self) -> None:
        """Main monitor loop for periodic tasks and reconnection."""
        logger.debug(
            "Reolink:%s monitor loop started",
            self.config.device.name,
        )
        backoff = 5.0
        iteration = 0

        while self._running:
            iteration += 1
            try:
                if not self._client.authenticated:
                    logger.debug(
                        "Reolink:%s not authenticated, attempting re-auth",
                        self.config.device.name,
                    )
                    await self._authenticate()

                # Keepalive ping (cmdId=93): some firmwares idle out an
                # otherwise-quiet authenticated session, which would silently
                # end the event subscription. Ping every loop so the session
                # stays alive regardless of event subscription state.
                if not await self._client.ping():
                    await self._client.close()
                    raise ReolinkError("Keepalive failed; reconnecting")

                if not self.config.events_enabled:
                    # No events to process, just keepalive
                    await asyncio.sleep(self.config.retry_delay)
                    continue

                # Stream discovery if needed
                if self._stream_url and not self._stream_url.success:
                    logger.debug(
                        "Reolink:%s stream URL invalid, re-discovering",
                        self.config.device.name,
                    )
                    await self._discover_stream()

                backoff = 5.0

                # Capabilities are static per camera and already applied during
                # startup discovery (_apply_discovery); no periodic refresh or
                # device-info polling is needed. The event listener handles
                # events, and the camera's push stream keeps the connection
                # alive. Pace the loop to avoid hammering the camera.
                await asyncio.sleep(self.config.retry_delay)

            except asyncio.CancelledError:
                logger.debug(
                    "Reolink:%s monitor loop cancelled",
                    self.config.device.name,
                )
                raise
            except Exception as error:
                self._set_error(error)
                logger.warning(
                    "Reolink:%s monitor error (iteration %d): %s",
                    self.config.device.name,
                    iteration,
                    error,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _event_loop(self) -> None:
        """Background loop that reads push event frames from TCP connection.

        The camera continuously pushes event frames (cmdId=33) over the
        TCP socket. This loop reads them and delivers events.
        """
        logger.info(
            "Reolink:%s event listener started",
            self.config.device.name,
        )

        while self._running:
            try:
                # Get the async frame iterator from the client
                frame_iter = self._client.event_frame_iterator

                async for cmd_id, body in frame_iter:
                    if not self._running:
                        break

                    try:
                        channel = self._client.host_channel_id
                        await self._process_event_frame(cmd_id, body, channel)
                    except Exception as exc:
                        logger.exception(
                            "Reolink:%s error processing event frame: %s",
                            self.config.device.name,
                            exc,
                        )

                if self._running:
                    await asyncio.sleep(self.config.event_retry_delay)

            except asyncio.CancelledError:
                logger.debug(
                    "Reolink:%s event listener cancelled",
                    self.config.device.name,
                )
                raise
            except Exception as exc:
                logger.warning(
                    "Reolink:%s event reader error (reconnecting): %s",
                    self.config.device.name,
                    exc,
                )
                # The client should handle reconnection
                await asyncio.sleep(self.config.event_retry_delay)

    async def _process_event_frame(self, cmd_id: int, body: bytes, channel: int = 0) -> None:
        """Process a single event frame from the camera.

        Args:
            cmd_id: Baichuan command ID from the frame header
            body: Encrypted event payload bytes
            channel: Host channel id used as the BC cipher decryption offset
        """
        now = datetime.now(tz=timezone.utc)

        dec = self._client.decryption_params
        await self._delivery_sink(
            RawPluginDelivery(
                plugin_id="reolink",
                device_id=self.config.device.id,
                area_id=self.config.device.area_id,
                received_at=now,
                payload=body,
                source="reolink:events",
                media_type="application/octet-stream",
                artifact_type="event_frame",
                metadata={
                    "kind": "raw_event_frame",
                    "integration": "reolink",
                    "command_id": cmd_id,
                    "channel": channel,
                    "nonce": str(dec.get("nonce", "")),
                    "use_aes": bool(dec.get("use_aes", False)),
                },
            )
        )

        self._status = replace(
            self._status,
            messages_received=self._status.messages_received + 1,
            last_message_at=now,
            error=None,
        )

    def decode_event_frame(
        self,
        cmd_id: int,
        body: bytes,
        channel: int,
        *,
        nonce: str | None = None,
        use_aes: bool | None = None,
    ) -> list[ReolinkEvent]:
        """Interpret a previously preserved frame using this session's cipher state."""
        dec = self._client.decryption_params
        frame_nonce = nonce if nonce is not None else str(dec.get("nonce", ""))
        frame_uses_aes = use_aes if use_aes is not None else bool(dec.get("use_aes", False))
        if cmd_id == 33:
            return parse_alarm_event_frame(
                body,
                channel=channel,
                nonce=frame_nonce,
                password=str(dec.get("password", "")),
                use_aes=frame_uses_aes,
            )
        if cmd_id == 252:
            event = parse_battery_status_frame(
                body,
                channel=channel,
                nonce=frame_nonce,
                password=str(dec.get("password", "")),
                use_aes=frame_uses_aes,
            )
            return [event] if event is not None else []
        return []

    def _set_error(self, error: Exception) -> None:
        """Update status state/error based on the exception type."""
        if isinstance(error, ReolinkLoginError):
            state = PluginInstanceState.FAILED
            message = "Authentication failed"
        elif isinstance(error, ReolinkStreamError):
            state = PluginInstanceState.FAILED
            message = f"Stream error: {error}"
        elif isinstance(error, ReolinkError):
            state = PluginInstanceState.STARTING
            message = str(error)[:200]
        else:
            state = PluginInstanceState.STARTING
            message = error.__class__.__name__
        self._status = replace(self._status, state=state, error=message)
        logger.debug(
            "Reolink:%s error set: state=%s message=%s",
            self.config.device.name,
            state,
            message,
        )

    def _refresh_status(self) -> None:
        """Recompute and persist the connection's capabilities and status."""
        if not self._discovered:
            return
        capabilities = ["discovery"]
        if self._stream_url and self._stream_url.success:
            capabilities.append("media")
        if self.config.events_enabled:
            capabilities.append("events")
        if self._snapshot_supported:
            capabilities.append("snapshots")
        self._status = replace(
            self._status,
            state=PluginInstanceState.RUNNING,
            connected_at=self._status.connected_at or datetime.now(tz=timezone.utc),
            error=None,
            device_info=None,
            summary="Connected",
            capabilities=tuple(capabilities),
            details={
                "connected": True,
                "events_enabled": self.config.events_enabled,
                "stream_url": self._stream_url.main_stream_url if self._stream_url else "",
            },
        )
        logger.debug(
            "Reolink:%s status refreshed: capabilities=%s",
            self.config.device.name,
            capabilities,
        )


def device_config(
    value: Mapping[str, object],
) -> tuple[ReolinkDeviceConfig | None, str | None]:
    """Build and validate a ReolinkDeviceConfig from a device mapping, returning
    the config and an optional error message."""
    try:
        device = Device(**dict(value))
    except (TypeError, ValueError) as error:
        return None, f"Invalid Reolink configuration ({error.__class__.__name__})."

    config = device.get_config("reolink")
    settings = config.settings if config else {}
    host = config.settings.get("host", "") if config else ""
    api_port = config.port if config and config.port else DEFAULT_API_PORT

    if not device.id or not device.name or not device.area_id or not device.ip_address:
        return None, "Reolink requires Device ID, name, Area, and network address."
    if not device.username or not device.password:
        return None, "Reolink requires Device credentials."
    if not isinstance(api_port, int) or not 1 <= api_port <= 65535:
        return None, "Reolink API port must be between 1 and 65535."
    try:
        timeout = float(settings.get("timeout", 10.0))
    except (TypeError, ValueError):
        return None, "Reolink timeout must be a number."
    if timeout <= 0:
        return None, "Reolink timeout must be greater than zero."

    logger.debug(
        "Reolink device config validated: id=%s name=%s host=%s port=%d",
        device.id,
        device.name,
        host or device.ip_address,
        api_port,
    )
    return (
        ReolinkDeviceConfig(
            device=device,
            host=host or device.ip_address,
            api_port=api_port,
            timeout=timeout,
            events_enabled=bool(settings.get("events_enabled", False)),
            media_enabled=bool(settings.get("media_enabled", False)),
            event_retry_delay=float(settings.get("event_retry_delay", 5.0)),
            dedup_window=float(settings.get("dedup_window", 5.0)),
        ),
        None,
    )
