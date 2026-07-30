from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from episode.domain.models import Device, Event

if TYPE_CHECKING:
    from episode.storage.repository import Repository


class RecordingTargetResolver(Protocol):
    async def resolve(self, event: Event) -> list[Device]: ...


class AreaRecordingTargetResolver:
    """Select video devices that should record for an Event in one Area."""

    def __init__(self, repo: Repository):
        self._repo = repo

    async def resolve(self, event: Event) -> list[Device]:
        if not event.area_id:
            return []
        devices = await self._repo.list_devices(area_id=event.area_id)
        targets = []
        for device in devices:
            if "video" not in device.capabilities:
                continue
            video = device.get_config("video")
            mode = video.settings.get("recording_mode", "on_event") if video else "on_event"
            if mode == "on_episode" or (mode == "on_event" and device.id == event.device_id):
                targets.append(device)
        return targets
