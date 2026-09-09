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
        if event.eligible_recording_device_ids is not None:
            # Newly allowed Events carry an immutable target snapshot.  This
            # keeps delayed dispatch and restart reconstruction deterministic
            # after an operator changes the active profile.
            devices = []
            for device_id in event.eligible_recording_device_ids or []:
                device = await self._repo.get_device(device_id)
                if device:
                    devices.append(device)
            return devices
        else:
            # Legacy/manual Events predate capture decisions and retain the
            # established dynamic resolver behavior.
            devices = await self._repo.list_devices(area_id=event.area_id)
        targets = []
        for device in devices:
            video = device.get_config("video")
            if video is None:
                continue
            mode = video.settings.get("recording_mode", "on_event")
            if mode == "on_episode" or (mode == "on_event" and device.id == event.device_id):
                targets.append(device)
        return targets
