from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from time import monotonic

import httpx

from episode.domain.models import Device, DeviceDiscoveryUpdate
from episode.media.registry import CameraMedia, VideoSourceDescriptor
from episode.plugins.models import (
    PluginDeviceInfo,
    PluginDeviceUpdateSink,
    PluginInstanceState,
    PluginInstanceStatus,
    PluginMediaRegistry,
    RawPluginDelivery,
    RawPluginDeliverySink,
)
from episode.plugins.onvif.client import TEV, ONVIFClient, ONVIFDevice, ONVIFError

logger = logging.getLogger(__name__)

DEFAULT_PATH = "/onvif/device_service"


@dataclass(frozen=True)
class ONVIFDeviceConfig:
    device: Device
    protocol: str = "http"
    port: int = 80
    path: str = DEFAULT_PATH
    auth_mode: str = "digest_wsse"
    timeout: float = 15
    events_enabled: bool = False
    profile_token: str = ""
    relaxed_xml: bool = False


ClientFactory = Callable[[ONVIFDeviceConfig], ONVIFClient]


def _default_client_factory(config: ONVIFDeviceConfig) -> ONVIFClient:
    device = config.device
    return ONVIFClient(
        device.ip_address,
        device.username,
        device.password,
        protocol=config.protocol,
        port=config.port,
        path=config.path,
        auth_mode=config.auth_mode,
        timeout=config.timeout,
        relaxed_xml=config.relaxed_xml,
    )


class ONVIFDeviceConnection:
    """Own discovery, media registration, and optional pull-point Events."""

    def __init__(
        self,
        config: ONVIFDeviceConfig,
        delivery_sink: RawPluginDeliverySink,
        media: PluginMediaRegistry,
        device_update_sink: PluginDeviceUpdateSink,
        *,
        client_factory: ClientFactory = _default_client_factory,
        retry_delay: float = 30,
    ) -> None:
        self.config = config
        self._delivery_sink = delivery_sink
        self._media = media
        self._device_update_sink = device_update_sink
        self._client = client_factory(config)
        self._retry_delay = retry_delay
        self._discovered: ONVIFDevice | None = None
        self._task: asyncio.Task | None = None
        self._running = False
        self._subscribed = False
        self._subscription_url: str | None = None
        self._selected_profile_token = ""
        self._status = PluginInstanceStatus(
            id=config.device.id,
            name=config.device.name,
            state=PluginInstanceState.STARTING,
        )

    def status(self) -> PluginInstanceStatus:
        return self._status

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        try:
            await self._discover()
        except Exception as error:
            self._set_error(error)
            logger.warning("ONVIF:%s initial discovery failed: %s", self.config.device.name, error)
        self._task = asyncio.create_task(
            self._monitor(),
            name=f"onvif:{self.config.device.id}",
        )
        if not self.config.events_enabled:
            logger.info("ONVIF:%s Events disabled by Device policy", self.config.device.name)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await self._unsubscribe()
        await self._client.close()
        self._status = replace(self._status, state=PluginInstanceState.STOPPED)

    async def _discover(self) -> None:
        discovered = await self._client.discover()
        self._discovered = discovered
        profile = self._select_profile(discovered)
        self._selected_profile_token = profile.token if profile else ""
        self._media.unregister(self.config.device.id, source="onvif")
        if profile:
            device = self.config.device
            for index, candidate in enumerate(discovered.profiles, start=1):
                # Tokens are useful inside ONVIF, but the stable public choice uses
                # a digest so the API need not expose protocol identifiers.
                token_digest = hashlib.sha256(candidate.token.encode("utf-8")).hexdigest()[:16]
                profile_name = candidate.name
                if (
                    not isinstance(profile_name, str)
                    or not profile_name
                    or len(profile_name) > 120
                    or "://" in profile_name
                    or any(ord(character) < 32 for character in profile_name)
                ):
                    profile_name = f"Profile {index}"
                encoding = candidate.encoding
                if (
                    not isinstance(encoding, str)
                    or len(encoding) > 32
                    or "://" in encoding
                    or any(ord(character) < 32 for character in encoding)
                ):
                    encoding = ""
                self._media.register(
                    CameraMedia(
                        device_id=device.id,
                        stream_uri=candidate.stream_uri,
                        snapshot_uri=candidate.snapshot_uri,
                        username=device.username,
                        password=device.password,
                        profile_token=candidate.token,
                        source="onvif",
                        video_source=VideoSourceDescriptor(
                            id=f"onvif:{token_digest}",
                            name=profile_name,
                            provider="ONVIF",
                            protocol="rtsp",
                            metadata_kind="configured",
                            width=(
                                candidate.width
                                if isinstance(candidate.width, int)
                                and not isinstance(candidate.width, bool)
                                and 0 < candidate.width <= 16384
                                else None
                            ),
                            height=(
                                candidate.height
                                if isinstance(candidate.height, int)
                                and not isinstance(candidate.height, bool)
                                and 0 < candidate.height <= 16384
                                else None
                            ),
                            frame_rate=(
                                candidate.frame_rate
                                if isinstance(candidate.frame_rate, (int, float))
                                and not isinstance(candidate.frame_rate, bool)
                                and 0 < candidate.frame_rate <= 240
                                else None
                            ),
                            codec=encoding,
                            default=candidate.token == profile.token,
                        ),
                    )
                )
            await self._apply_discovery(profile)
        self._refresh_status()
        logger.info(
            "ONVIF:%s discovered %s %s (%d media profiles, %d Event topics)",
            self.config.device.name,
            discovered.manufacturer or "ONVIF",
            discovered.model or "Device",
            len(discovered.profiles),
            len(discovered.event_topics),
        )

    def _select_profile(self, discovered: ONVIFDevice):
        requested = self.config.profile_token
        if requested:
            match = next(
                (
                    profile
                    for profile in discovered.profiles
                    if profile.token == requested and profile.stream_uri
                ),
                None,
            )
            if match:
                return match
            logger.warning(
                "ONVIF:%s requested media profile %s was not advertised",
                self.config.device.name,
                requested,
            )
        return max(
            (profile for profile in discovered.profiles if profile.stream_uri),
            key=lambda profile: profile.width * profile.height,
            default=None,
        )

    async def _apply_discovery(self, profile) -> None:
        device = self.config.device
        capabilities = ["video", "events"]
        if profile.snapshot_uri and "snapshot" not in device.capabilities:
            capabilities.append("snapshot")
        if self._discovered and "Tamper" in self._discovered.event_topics:
            capabilities.append("tamper")
        if self._device_update_sink is not None:
            await self._device_update_sink(
                DeviceDiscoveryUpdate(
                    device_id=device.id,
                    integration_type="onvif",
                    capabilities=tuple(capabilities),
                    metadata={
                        "manufacturer": self._discovered.manufacturer,
                        "model": self._discovered.model,
                        "firmware_version": self._discovered.firmware_version,
                        "profile_token": profile.token,
                        "profiles": len(self._discovered.profiles),
                        "events": TEV in self._discovered.services,
                        "events_enabled": self.config.events_enabled,
                        "snapshot": bool(profile.snapshot_uri),
                    },
                )
            )

    async def _monitor(self) -> None:
        backoff = 5.0
        while self._running:
            try:
                if self._discovered is None:
                    await self._discover()
                if not self.config.events_enabled:
                    await asyncio.sleep(self._retry_delay)
                    continue
                events_url = self._discovered.services.get(TEV) if self._discovered else None
                if not events_url:
                    self._set_error(ONVIFError("Device did not advertise an Event service"))
                    await asyncio.sleep(self._retry_delay)
                    continue
                subscription_url = await self._client.create_pull_point(events_url)
                self._subscription_url = subscription_url
                self._subscribed = True
                backoff = 5.0
                self._refresh_status()
                renew_at = monotonic() + 60
                while self._running:
                    if monotonic() >= renew_at:
                        await self._client.renew(subscription_url)
                        renew_at = monotonic() + 60
                    root, raw = await self._client.pull_messages(subscription_url)
                    if root.findall(".//{http://docs.oasis-open.org/wsn/b-2}NotificationMessage"):
                        await self._preserve(raw)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._subscribed = False
                await self._unsubscribe()
                self._set_error(error)
                logger.warning(
                    "ONVIF:%s Event subscription error: %s", self.config.device.name, error
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _preserve(self, raw: bytes) -> None:
        received_at = datetime.now(tz=timezone.utc)
        await self._delivery_sink(
            RawPluginDelivery(
                plugin_id="onvif",
                device_id=self.config.device.id,
                area_id=self.config.device.area_id,
                received_at=received_at,
                payload=raw,
                source="onvif:events",
                media_type="application/soap+xml",
                artifact_type="event_batch",
                metadata={"kind": "pull_response", "integration": "onvif"},
            )
        )
        self._status = replace(
            self._status,
            messages_received=self._status.messages_received + 1,
            last_message_at=received_at,
            error=None,
        )

    async def _unsubscribe(self) -> None:
        subscription_url = self._subscription_url
        self._subscription_url = None
        self._subscribed = False
        if not subscription_url:
            return
        try:
            await self._client.unsubscribe(subscription_url)
        except Exception:
            logger.debug("ONVIF:%s subscription already unavailable", self.config.device.name)

    def _set_error(self, error: Exception) -> None:
        if isinstance(error, httpx.HTTPStatusError):
            message = f"HTTP {error.response.status_code}"
            state = (
                PluginInstanceState.FAILED
                if error.response.status_code in {401, 403}
                else PluginInstanceState.STARTING
            )
        elif isinstance(error, (httpx.HTTPError, ONVIFError)):
            message = str(error)[:200]
            state = PluginInstanceState.STARTING
        else:
            message = error.__class__.__name__
            state = PluginInstanceState.STARTING
        self._status = replace(self._status, state=state, error=message)

    def _refresh_status(self) -> None:
        discovered = self._discovered
        if discovered is None:
            return
        profiles = [
            {
                "token": profile.token,
                "name": profile.name,
                "encoding": profile.encoding,
                "width": profile.width,
                "height": profile.height,
                "frame_rate": profile.frame_rate,
                "snapshot": bool(profile.snapshot_uri),
            }
            for profile in discovered.profiles
        ]
        capabilities = ["discovery"]
        if profiles:
            capabilities.append("media")
        if any(profile["snapshot"] for profile in profiles):
            capabilities.append("snapshots")
        if discovered.event_topics or self.config.events_enabled:
            capabilities.append("events")
        healthy = not self.config.events_enabled or self._subscribed
        event_policy = "Events enabled" if self.config.events_enabled else "Events disabled"
        self._status = replace(
            self._status,
            state=(PluginInstanceState.RUNNING if healthy else PluginInstanceState.STARTING),
            connected_at=self._status.connected_at or datetime.now(tz=timezone.utc),
            error=None,
            device_info=PluginDeviceInfo(
                manufacturer=discovered.manufacturer or None,
                model=discovered.model or None,
                firmware_version=discovered.firmware_version or None,
            ),
            summary=f"Connected · {len(profiles)} media profiles · {event_policy}",
            capabilities=tuple(capabilities),
            details={
                "connected": True,
                "subscribed": self._subscribed,
                "events_enabled": self.config.events_enabled,
                "profiles": profiles,
                "selected_profile": self._selected_profile_token,
                "event_topics": discovered.event_topics,
            },
        )


def device_config(value: Mapping[str, object]) -> tuple[ONVIFDeviceConfig | None, str | None]:
    try:
        device = Device(**dict(value))
    except (TypeError, ValueError) as error:
        return None, f"Invalid ONVIF Device configuration ({error.__class__.__name__})."
    config = device.get_config("onvif")
    settings = config.settings if config else {}
    protocol = config.protocol if config and config.protocol else "http"
    port = config.port if config and config.port is not None else 80
    path = config.path if config and config.path else DEFAULT_PATH
    if not device.id or not device.name or not device.area_id or not device.ip_address:
        return None, "ONVIF requires Device ID, name, Area, and network address."
    if not device.username or not device.password:
        return None, "ONVIF requires Device credentials."
    if protocol not in {"http", "https"}:
        return None, "ONVIF protocol must be http or https."
    if not isinstance(port, int) or not 1 <= port <= 65535:
        return None, "ONVIF port must be between 1 and 65535."
    if not path.startswith("/"):
        return None, "ONVIF path must begin with /."
    try:
        timeout = float(settings.get("timeout", 15))
    except (TypeError, ValueError):
        return None, "ONVIF timeout must be a number."
    if timeout <= 0:
        return None, "ONVIF timeout must be greater than zero."
    return (
        ONVIFDeviceConfig(
            device=device,
            protocol=protocol,
            port=port,
            path=path,
            auth_mode=str(settings.get("auth_mode", "digest_wsse")),
            timeout=timeout,
            events_enabled=bool(settings.get("events_enabled", False)),
            profile_token=str(settings.get("profile_token", "")),
            relaxed_xml=bool(settings.get("relaxed_xml", False)),
        ),
        None,
    )
