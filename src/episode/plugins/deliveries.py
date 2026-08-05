from __future__ import annotations

import asyncio
import re
from dataclasses import asdict

from episode.domain.models import IngestionReceipt, ReceiptStatus
from episode.engine.bus import EventBus, Message
from episode.plugins.models import RawPluginDelivery
from episode.storage.files import describe_artifact, save_bytes
from episode.storage.repository import Repository

_SAFE_COMPONENT = re.compile(r"[^a-zA-Z0-9_.-]+")


def _safe_component(value: str, fallback: str) -> str:
    component = _SAFE_COMPONENT.sub("-", value).strip(".-")
    return component[:80] or fallback


class RawPluginDeliveryStore:
    """Core-owned persistence boundary for uninterpreted plugin notifications."""

    def __init__(
        self,
        data_dir: str,
        repository: Repository,
        bus: EventBus | None = None,
    ):
        self._data_dir = data_dir
        self._repository = repository
        self._bus = bus

    async def __call__(self, delivery: RawPluginDelivery) -> None:
        plugin_id = _safe_component(delivery.plugin_id, "plugin")
        device_id = _safe_component(delivery.device_id, "device")
        path = await asyncio.to_thread(
            save_bytes,
            self._data_dir,
            f"orphans/plugin-deliveries/{plugin_id}/{device_id}",
            delivery.payload,
            prefix="notification",
            extension=".bin",
        )
        metadata = {
            "plugin_id": delivery.plugin_id,
            "device_id": delivery.device_id,
            **dict(delivery.metadata),
        }
        if delivery.event:
            metadata["interpreted_event_type"] = delivery.event.event_type
        artifact = await asyncio.to_thread(
            describe_artifact,
            path,
            "plugin_notification",
            "application/octet-stream",
            metadata=metadata,
        )
        receipt = IngestionReceipt(
            source=f"plugin:{delivery.plugin_id}",
            received_at=delivery.received_at,
            observed_at=delivery.event.timestamp if delivery.event else None,
            status=ReceiptStatus.ACCEPTED,
            device_id=delivery.device_id,
            area_id=delivery.area_id,
            metadata=metadata,
        )
        if delivery.event and self._bus:
            event = delivery.event
            await self._bus.publish(
                Message(
                    type="event.received",
                    data={
                        "event": {
                            "device_id": delivery.device_id,
                            "area_id": delivery.area_id,
                            "timestamp": event.timestamp,
                            "event_type": event.event_type,
                            "event_state": event.event_state,
                            "source": event.source or f"plugin:{delivery.plugin_id}",
                            "dedup_key": event.dedup_key,
                            "raw_payload_path": path,
                            "metadata": dict(event.metadata),
                        },
                        "artifact": asdict(artifact),
                        "receipt": asdict(receipt),
                    },
                )
            )
            return
        await self._repository.persist_delivery(artifact, receipt)
