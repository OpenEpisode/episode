from __future__ import annotations

from episode.ingestion.models import IngressDelivery
from episode.ingestion.service import IngestionService
from episode.plugins.models import RawPluginDelivery


class RawPluginDeliveryStore:
    """Adapt plugin callback buffers to the core raw-first ingress boundary."""

    def __init__(self, ingestion: IngestionService):
        self._ingestion = ingestion

    async def __call__(self, delivery: RawPluginDelivery) -> None:
        await self._ingestion.accept(
            IngressDelivery(
                source=delivery.source or f"plugin:{delivery.plugin_id}",
                transport="plugin",
                received_at=delivery.received_at,
                payload=delivery.payload,
                media_type=delivery.media_type,
                artifact_type=delivery.artifact_type,
                device_id=delivery.device_id,
                area_id=delivery.area_id,
                metadata={
                    "plugin_id": delivery.plugin_id,
                    "device_id": delivery.device_id,
                    **dict(delivery.metadata),
                },
            )
        )
