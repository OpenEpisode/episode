from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from time import monotonic

import httpx

from episode.connectors.base import Connector
from episode.connectors.onvif.client import TEV, ONVIFClient, ONVIFDevice, ONVIFError
from episode.connectors.onvif.parser import (
    ONVIFNotification,
    ONVIFStateTracker,
    parse_notifications,
)
from episode.domain.models import CapabilityConfig, IngestionReceipt, ReceiptStatus
from episode.engine.bus import EventBus, Message
from episode.media.registry import CameraMedia, MediaRegistry
from episode.storage.files import describe_artifact, save_payload

logger = logging.getLogger(__name__)


class ONVIFConnector(Connector):
    def __init__(
        self,
        name: str,
        bus: EventBus,
        config: dict,
        app_config,
        device,
        repo,
        media: MediaRegistry,
    ):
        super().__init__(name, bus, config)
        self._app_config = app_config
        self._configured_device = device
        self._repo = repo
        self._media = media
        self._client = ONVIFClient(
            device.ip_address,
            device.username,
            device.password,
            protocol=config.get("protocol", "http"),
            port=config.get("port", 80),
            path=config.get("path", "/onvif/device_service"),
            auth_mode=config.get("auth_mode", "digest_wsse"),
            timeout=float(config.get("timeout", 15)),
        )
        self._onvif_device: ONVIFDevice | None = None
        self._task: asyncio.Task | None = None
        self._connected = False
        self._subscribed = False
        self._subscription_url: str | None = None
        self._last_error: str | None = None
        self._last_event: str | None = None
        self._received = 0
        self._suppressed = 0
        self._state_tracker = ONVIFStateTracker()
        self._events_enabled = bool(config.get("events_enabled", False))

    async def start(self) -> None:
        self._running = True
        try:
            await self._discover()
        except Exception as error:
            self._set_error(error)
            logger.warning("%s: initial discovery failed: %s", self.name, error)
        self._task = asyncio.create_task(self._monitor())
        if not self._events_enabled:
            logger.info("%s: ONVIF events disabled by device policy", self.name)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self._unsubscribe()
        await self._client.close()

    async def _discover(self) -> None:
        self._onvif_device = await self._client.discover()
        self._connected = True
        self._last_error = None
        profile = self._select_profile(self._onvif_device)
        if profile:
            self._media.register(
                CameraMedia(
                    device_id=self._configured_device.id,
                    stream_uri=profile.stream_uri,
                    snapshot_uri=profile.snapshot_uri,
                    username=self._configured_device.username,
                    password=self._configured_device.password,
                    profile_token=profile.token,
                    source="onvif",
                )
            )
            await self._apply_discovered_capabilities(profile)
        logger.info(
            "%s: discovered %s %s (%d media profiles, %d event topics)",
            self.name,
            self._onvif_device.manufacturer or "ONVIF",
            self._onvif_device.model or "camera",
            len(self._onvif_device.profiles),
            len(self._onvif_device.event_topics),
        )

    def _select_profile(self, device: ONVIFDevice):
        requested = self._config.get("profile_token")
        if requested:
            match = next(
                (profile for profile in device.profiles if profile.token == requested), None
            )
            if match:
                return match
            logger.warning(
                "%s: requested media profile %s was not advertised", self.name, requested
            )
        return max(
            device.profiles, key=lambda profile: profile.width * profile.height, default=None
        )

    async def _apply_discovered_capabilities(self, profile) -> None:
        for capability in ("video", "events"):
            if capability not in self._configured_device.capabilities:
                self._configured_device.capabilities.append(capability)
        if profile.snapshot_uri and "snapshot" not in self._configured_device.capabilities:
            self._configured_device.capabilities.append("snapshot")
        if self._onvif_device and "Tamper" in self._onvif_device.event_topics:
            if "tamper" not in self._configured_device.capabilities:
                self._configured_device.capabilities.append("tamper")

        existing = self._configured_device.get_config("video")
        recording_mode = (
            existing.settings.get("recording_mode", "on_event") if existing else "on_event"
        )
        if existing and existing.settings.get("origin") != "onvif":
            settings = dict(existing.settings)
            settings["recording_mode"] = recording_mode
            self._configured_device.configs["video"] = CapabilityConfig(
                protocol=existing.protocol,
                port=existing.port,
                path=existing.path,
                settings=settings,
            )
        else:
            self._configured_device.configs["video"] = CapabilityConfig(
                settings={"recording_mode": recording_mode}
            )
        self._configured_device.metadata["onvif"] = {
            "manufacturer": self._onvif_device.manufacturer,
            "model": self._onvif_device.model,
            "firmware_version": self._onvif_device.firmware_version,
            "profile_token": profile.token,
            "profiles": len(self._onvif_device.profiles),
            "events": TEV in self._onvif_device.services,
            "events_enabled": self._events_enabled,
            "snapshot": bool(profile.snapshot_uri),
        }
        await self._repo.upsert_device(self._configured_device)

    async def _monitor(self) -> None:
        if not self._events_enabled:
            while self._running:
                if not self._onvif_device:
                    try:
                        await self._discover()
                    except Exception as error:
                        self._set_error(error)
                        logger.warning("%s: discovery retry failed: %s", self.name, error)
                await asyncio.sleep(30)
            return

        backoff = 5
        while self._running:
            try:
                if not self._onvif_device:
                    await self._discover()
                events_url = self._onvif_device.services.get(TEV) if self._onvif_device else None
                if not events_url:
                    await asyncio.sleep(30)
                    continue
                subscription_url = await self._client.create_pull_point(events_url)
                backoff = 5
                self._subscription_url = subscription_url
                self._connected = True
                self._last_error = None
                self._subscribed = True
                renew_at = monotonic() + 60
                while self._running:
                    if monotonic() >= renew_at:
                        await self._client.renew(subscription_url)
                        renew_at = monotonic() + 60
                    root, raw = await self._client.pull_messages(subscription_url)
                    await self._ingest_response(root, raw)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._subscribed = False
                await self._unsubscribe()
                self._set_error(error)
                logger.warning("%s: event subscription error: %s", self.name, error)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _unsubscribe(self) -> None:
        subscription_url = self._subscription_url
        self._subscription_url = None
        self._subscribed = False
        if not subscription_url:
            return
        try:
            await self._client.unsubscribe(subscription_url)
        except Exception:
            logger.debug("%s: subscription already unavailable", self.name)

    async def _ingest_response(self, root, raw: bytes) -> None:
        notifications = parse_notifications(root)
        if not notifications:
            return

        accepted = []
        ignored = []
        suppressed = []
        for notification in notifications:
            if self._state_tracker.is_transition(notification):
                accepted.append(notification)
            elif notification.is_initial_value or not notification.event_type:
                ignored.append(notification)
            else:
                suppressed.append(notification)

        topics = sorted({notification.topic for notification in notifications})
        artifact = self._artifact(raw, {"topics": topics})
        for notification in accepted:
            await self._publish_event(artifact, notification)

        self._suppressed += len(suppressed)
        if not accepted:
            reason = "repeated_state" if suppressed and not ignored else "initial_or_unmapped"
            await self._publish_ignored_response(
                artifact,
                notifications,
                reason=reason,
            )

    def _artifact(self, raw: bytes, metadata: dict):
        path = save_payload(
            self._app_config.orphans_dir,
            "events",
            raw,
            prefix="onvif",
        )
        return describe_artifact(
            path,
            "event_payload",
            "application/soap+xml",
            metadata={"protocol": "onvif", **metadata},
        )

    async def _publish_event(self, artifact, notification: ONVIFNotification) -> None:
        receipt = IngestionReceipt(
            source="onvif:events",
            observed_at=notification.timestamp,
            artifact_id=artifact.id,
            device_id=self._configured_device.id,
            area_id=self._configured_device.area_id,
            metadata={
                "topic": notification.topic,
                "property_operation": notification.property_operation,
            },
        )
        event = {
            "device_id": self._configured_device.id,
            "area_id": self._configured_device.area_id,
            "timestamp": notification.timestamp,
            "event_type": notification.event_type,
            "event_state": notification.event_state.value,
            "source": "onvif:events",
            "raw_payload_path": artifact.file_path,
            "metadata": {"onvif_topic": notification.topic, "onvif_items": notification.items},
        }
        await self._bus.publish(
            Message(
                type="event.received",
                data={
                    "event": event,
                    "artifact": asdict(artifact),
                    "receipt": asdict(receipt),
                },
            )
        )
        self._received += 1
        self._last_event = notification.timestamp.isoformat()

    async def _publish_ignored_response(
        self,
        artifact,
        notifications: list[ONVIFNotification],
        *,
        reason: str,
    ) -> None:
        topics = sorted({notification.topic for notification in notifications})
        receipt = IngestionReceipt(
            source="onvif:events",
            status=ReceiptStatus.IGNORED,
            artifact_id=artifact.id,
            device_id=self._configured_device.id,
            area_id=self._configured_device.area_id,
            metadata={"reason": reason, "topics": topics},
        )
        await self._bus.publish(
            Message(
                type="receipt.received",
                data={"artifact": asdict(artifact), "receipt": asdict(receipt)},
            )
        )

    def _set_error(self, error: Exception) -> None:
        self._connected = False
        if isinstance(error, httpx.HTTPStatusError):
            self._last_error = f"HTTP {error.response.status_code}"
        elif isinstance(error, (httpx.HTTPError, ONVIFError)):
            self._last_error = str(error)[:200]
        else:
            self._last_error = error.__class__.__name__

    def status(self) -> dict:
        profiles = []
        if self._onvif_device:
            profiles = [
                {
                    "token": profile.token,
                    "name": profile.name,
                    "encoding": profile.encoding,
                    "width": profile.width,
                    "height": profile.height,
                    "snapshot": bool(profile.snapshot_uri),
                }
                for profile in self._onvif_device.profiles
            ]
        return {
            **super().status(),
            "device_id": self._configured_device.id,
            "connected": self._connected,
            "healthy": self._connected and (not self._events_enabled or self._subscribed),
            "subscribed": self._subscribed,
            "events_enabled": self._events_enabled,
            "manufacturer": self._onvif_device.manufacturer if self._onvif_device else "",
            "model": self._onvif_device.model if self._onvif_device else "",
            "firmware_version": (self._onvif_device.firmware_version if self._onvif_device else ""),
            "profiles": profiles,
            "selected_profile": (
                self._configured_device.metadata.get("onvif", {}).get("profile_token", "")
            ),
            "event_topics": self._onvif_device.event_topics if self._onvif_device else [],
            "events_received": self._received,
            "events_suppressed": self._suppressed,
            "last_event": self._last_event,
            "last_error": self._last_error,
        }
