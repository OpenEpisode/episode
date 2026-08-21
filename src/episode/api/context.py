from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from episode.api.projections import public_event
from episode.api.runtime import OperationalView
from episode.api.schemas import EventResponse
from episode.inventory import DeviceValidationService, InventoryService
from episode.media.previews import CurrentViewService
from episode.media.timelapse import TimelapseService


@dataclass(slots=True)
class ApiContext:
    """Explicit application services available to HTTP route groups."""

    repository: Any
    data_dir: str = ""
    snapshot_window: int = 1
    timelapses: TimelapseService | None = None
    operations: OperationalView | None = None
    inventory: InventoryService | None = None
    validator: DeviceValidationService | None = None
    current_views: CurrentViewService | None = None

    def __post_init__(self) -> None:
        if self.timelapses is None:
            self.timelapses = TimelapseService(self.repository, self.data_dir)

    def public_integrations(self) -> list[dict]:
        return self.operations.integrations(detailed=False) if self.operations else []

    async def public_event(self, event, integrations: list[dict] | None = None) -> EventResponse:
        event_id = event.get("id") if isinstance(event, dict) else event.id
        receipts = await self.repository.list_ingestion_receipts(event_id=event_id)
        return public_event(
            event,
            receipts,
            self.public_integrations() if integrations is None else integrations,
        )

    async def public_events(self, events) -> list[EventResponse]:
        integrations = self.public_integrations()
        return await asyncio.gather(
            *(self.public_event(event, integrations=integrations) for event in events)
        )
