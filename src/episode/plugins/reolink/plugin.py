from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone

from episode.domain.models import ReceiptStatus
from episode.ingestion.models import EventObservation, IngressHandlerResult, StoredIngressEnvelope
from episode.ingestion.router import IngressHandlerRegistration
from episode.plugins.models import (
    PluginContext,
    PluginInstanceState,
    PluginInstanceStatus,
    PluginState,
    PluginStatus,
    RawPluginDelivery,
)
from episode.plugins.reolink.device import ReolinkDeviceConnection, device_config

logger = logging.getLogger(__name__)

PLUGIN_ID = "reolink"
PLUGIN_NAME = "Reolink"
PLUGIN_KIND = "device-integration"
HANDLER_ID = "reolink-events"


def _configured_devices(devices: tuple[Mapping[str, object], ...]) -> list[Mapping[str, object]]:
    """Return the subset of configured devices that have an enabled Reolink
    config."""
    return [
        device
        for device in devices
        if "reolink" in device.get("configs", {}) and device.get("enabled", True)
    ]


class _ReolinkEventTracker:
    """Time-aware edge detector: emits on state changes or after a quiet
    interval, suppresses the rapid repeated frames cameras push during a
    detection."""

    def __init__(self, window: float = 1.0) -> None:
        """Initialize the tracker with the suppression window in seconds."""
        self._window = window
        self._states: dict[str, tuple[str, datetime | None]] = {}

    def is_transition(self, event_type: str, event_state: str, at: datetime | None = None) -> bool:
        """Return True if this (type, state) is a new transition, False if it
        is a repeat within the suppression window."""
        at = at or datetime.now(tz=timezone.utc)
        previous_state, previous_at = self._states.get(event_type, ("", None))
        self._states[event_type] = (event_state, at)
        if previous_state != event_state:
            return True
        if previous_at is None:
            return True
        return (at - previous_at).total_seconds() >= self._window


class ReolinkPlugin:
    def __init__(self, context: PluginContext, *, connection_factory=ReolinkDeviceConnection):
        """Initialize the Reolink plugin from the plugin context."""
        self._router = context.ingress_router
        self._delivery_sink = context.raw_delivery_sink
        self._device_update_sink = context.device_update_sink
        self._media_registry = context.media_registry
        self._configured_devices = _configured_devices(context.configured_devices)
        self._connection_factory = connection_factory
        self._connections: list[ReolinkDeviceConnection] = []
        self._invalid_instances: list[PluginInstanceStatus] = []
        self._event_counts: dict[str, int] = {}
        self._suppressed_counts: dict[str, int] = {}
        self._last_events: dict[str, str] = {}
        self._trackers: dict[str, _ReolinkEventTracker] = {}
        self._registered = False
        logger.info(
            "Reolink plugin initialized: %d configured device(s), "
            "router=%s delivery=%s device_update=%s",
            len(self._configured_devices),
            self._router is not None,
            self._delivery_sink is not None,
            self._device_update_sink is not None,
        )

    @staticmethod
    def _matches(envelope: StoredIngressEnvelope) -> bool:
        """Return True if the envelope is a Reolink plugin transport delivery."""
        return envelope.transport == "plugin" and envelope.metadata.get("plugin_id") == PLUGIN_ID

    async def _handle(self, envelope: StoredIngressEnvelope) -> IngressHandlerResult:
        """Expand preserved frames, then interpret their derived notifications."""
        kind = envelope.metadata.get("kind")
        if kind == "raw_event_frame":
            command_id = envelope.metadata.get("command_id")
            channel = envelope.metadata.get("channel", 0)
            if not isinstance(command_id, int) or not isinstance(channel, int):
                return IngressHandlerResult(
                    claimed=True,
                    status=ReceiptStatus.REJECTED,
                    metadata={"reason": "invalid_frame_metadata"},
                )
            connection = next(
                (item for item in self._connections if item.config.device.id == envelope.device_id),
                None,
            )
            if connection is None:
                return IngressHandlerResult(
                    claimed=True,
                    status=ReceiptStatus.REJECTED,
                    metadata={"reason": "device_connection_unavailable"},
                )
            if command_id == 252:
                return IngressHandlerResult(
                    claimed=True,
                    status=ReceiptStatus.IGNORED,
                    metadata={"reason": "device_telemetry", "command_id": command_id},
                )
            nonce = envelope.metadata.get("nonce")
            use_aes = envelope.metadata.get("use_aes")
            events = connection.decode_event_frame(
                command_id,
                envelope.payload,
                channel,
                nonce=nonce if isinstance(nonce, str) else None,
                use_aes=use_aes if isinstance(use_aes, bool) else None,
            )
            if self._delivery_sink is None:
                return IngressHandlerResult(
                    claimed=True,
                    status=ReceiptStatus.REJECTED,
                    metadata={"reason": "derived_delivery_storage_unavailable"},
                )
            for index, event in enumerate(events):
                await self._delivery_sink(
                    RawPluginDelivery(
                        plugin_id=PLUGIN_ID,
                        device_id=envelope.device_id,
                        area_id=envelope.area_id,
                        received_at=envelope.received_at,
                        payload=json.dumps(
                            event.raw_payload or event.metadata,
                            sort_keys=True,
                            default=str,
                        ).encode("utf-8"),
                        source="reolink:events",
                        media_type="application/json",
                        artifact_type="derived_event_notification",
                        metadata={
                            "kind": "event_notification",
                            "integration": "reolink",
                            "parent_receipt_id": envelope.receipt_id,
                            "notification_index": index,
                            "event_type": event.event_type,
                            "event_state": event.event_state,
                            "observed_at": event.timestamp.isoformat(),
                            "channel": event.channel,
                            "event_id": event.event_id,
                        },
                    )
                )
            return IngressHandlerResult(
                claimed=True,
                status=ReceiptStatus.IGNORED,
                metadata={
                    "reason": "expanded_to_notifications" if events else "no_notifications",
                    "notification_count": len(events),
                },
            )

        if kind != "event_notification":
            return IngressHandlerResult(claimed=False)

        event_type = envelope.metadata.get("event_type")
        event_state = envelope.metadata.get("event_state")
        if (
            not isinstance(event_type, str)
            or not event_type
            or event_state not in {"active", "inactive"}
        ):
            logger.debug(
                "Reolink handler: envelope missing event type/state for device %s",
                envelope.device_id,
            )
            return IngressHandlerResult(
                claimed=True,
                status=ReceiptStatus.REJECTED,
                metadata={"reason": "missing_event_fields"},
            )

        # Suppress repeated identical states (time-aware edge detection).
        tracker = self._trackers.setdefault(envelope.device_id, _ReolinkEventTracker())
        if not tracker.is_transition(event_type, event_state, envelope.received_at):
            self._suppressed_counts[envelope.device_id] = (
                self._suppressed_counts.get(envelope.device_id, 0) + 1
            )
            return IngressHandlerResult(
                claimed=True,
                status=ReceiptStatus.IGNORED,
                metadata={"reason": "repeated_state", "event_type": event_type},
            )

        self._event_counts[envelope.device_id] = self._event_counts.get(envelope.device_id, 0) + 1
        self._last_events[envelope.device_id] = envelope.received_at.isoformat()
        logger.debug(
            "Reolink handler: interpreted event for device %s (total: %d)",
            envelope.device_id,
            self._event_counts[envelope.device_id],
        )

        observed_at = envelope.received_at
        raw_observed_at = envelope.metadata.get("observed_at")
        if isinstance(raw_observed_at, str):
            try:
                observed_at = datetime.fromisoformat(raw_observed_at)
            except ValueError:
                pass

        return IngressHandlerResult(
            claimed=True,
            event=EventObservation(
                timestamp=observed_at,
                event_type=event_type,
                event_state=event_state,
                source="reolink:events",
                device_id=envelope.device_id,
                area_id=envelope.area_id,
                metadata={
                    "integration": "reolink",
                    "channel": envelope.metadata.get("channel", 0),
                    "event_id": envelope.metadata.get("event_id", ""),
                },
            ),
            metadata={"interpreted": True, "source": "reolink:events"},
        )

    def status(self) -> PluginStatus:
        """Aggregate per-connection status into the overall plugin status."""
        metrics = self._router.status(HANDLER_ID) if self._router else None
        instances = [*self._invalid_instances]
        for connection in self._connections:
            status = connection.status()
            details = {
                **dict(status.details),
                "events_received": self._event_counts.get(status.id, 0),
                "events_suppressed": self._suppressed_counts.get(status.id, 0),
                "last_event": self._last_events.get(status.id),
            }
            instances.append(replace(status, details=details))

        running = sum(item.state == PluginInstanceState.RUNNING for item in instances)

        if not self._registered or self._router is None:
            state = PluginState.FAILED
            error = "Reolink ingress routing is unavailable."
        elif not instances or running == len(instances):
            state = PluginState.READY
            error = None
        elif running:
            state = PluginState.DEGRADED
            error = f"{len(instances) - running} Reolink device connection(s) unavailable."
        elif any(item.state == PluginInstanceState.STARTING for item in instances):
            state = PluginState.VALIDATING
            error = "Reolink device connections are starting or reconnecting."
        else:
            state = PluginState.FAILED
            error = "No configured Reolink device connections are available."

        if error is not None:
            logger.warning(
                "Reolink plugin status: state=%s error=%s",
                state,
                error or "none",
            )
        return PluginStatus(
            id=PLUGIN_ID,
            name=PLUGIN_NAME,
            kind=PLUGIN_KIND,
            state=state,
            error=error,
            instances=tuple(instances),
            metrics=metrics or {},
        )

    async def start(self) -> None:
        """Register the ingress handler and start all device connections."""
        if self._router is None or self._registered:
            logger.debug(
                "Reolink plugin start skipped: router=%s registered=%s",
                self._router is not None,
                self._registered,
            )
            return
        logger.info(
            "Reolink plugin starting with %d device(s)",
            len(self._configured_devices),
        )
        self._router.register(
            IngressHandlerRegistration(id=HANDLER_ID, matcher=self._matches, handler=self._handle)
        )
        self._registered = True

        if self._device_update_sink is None:
            self._invalid_instances = [
                PluginInstanceStatus(
                    id=str(device.get("id") or "unknown"),
                    name=str(device.get("name") or device.get("id") or "Unknown Device"),
                    state=PluginInstanceState.FAILED,
                    error="Device update persistence is unavailable.",
                )
                for device in self._configured_devices
            ]
            logger.warning(
                "Reolink plugin: device update sink unavailable, %d device(s) marked FAILED",
                len(self._configured_devices),
            )
            return

        for device in self._configured_devices:
            config, error = device_config(device)
            if config is None:
                logger.warning(
                    "Reolink plugin: invalid device config for id=%s: %s",
                    device.get("id", "unknown"),
                    error,
                )
                self._invalid_instances.append(
                    PluginInstanceStatus(
                        id=str(device.get("id") or "unknown"),
                        name=str(device.get("name") or device.get("id") or "Unknown Device"),
                        state=PluginInstanceState.FAILED,
                        error=error,
                    )
                )
                continue
            logger.debug(
                "Reolink plugin: creating connection for device %s (%s)",
                config.device.name,
                config.device.id,
            )
            self._connections.append(
                self._connection_factory(
                    config,
                    self._delivery_sink,
                    self._device_update_sink,
                    media_registry=self._media_registry,
                )
            )

        await asyncio.gather(*(connection.start() for connection in self._connections))
        logger.info(
            "Reolink plugin: all %d device connection(s) started",
            len(self._connections),
        )

    async def stop(self) -> None:
        """Stop all device connections and unregister the ingress handler."""
        logger.info(
            "Reolink plugin stopping: %d connection(s)",
            len(self._connections),
        )
        await asyncio.gather(
            *(connection.stop() for connection in reversed(self._connections)),
            return_exceptions=True,
        )
        self._connections.clear()
        if self._router is not None and self._registered:
            self._router.unregister(HANDLER_ID)
            logger.info("Reolink ingress handler unregistered: id=%s", HANDLER_ID)
        self._registered = False
        logger.info("Reolink plugin stopped")
