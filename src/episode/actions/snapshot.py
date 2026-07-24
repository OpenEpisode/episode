from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from time import monotonic
from typing import TYPE_CHECKING

from episode.domain.models import Evidence, IngestionReceipt
from episode.engine.bus import EventBus, Message
from episode.media.registry import MediaRegistry
from episode.storage.files import describe_artifact, save_bytes

if TYPE_CHECKING:
    from episode.config import EpisodeConfig

logger = logging.getLogger(__name__)


class SnapshotEngine:
    """Vendor-neutral snapshot action backed by discovered camera media."""

    def __init__(self, bus: EventBus, media: MediaRegistry, config: EpisodeConfig):
        self._bus = bus
        self._media = media
        self._config = config
        self._running = False
        self._tasks: set[asyncio.Task] = set()
        self._capturing: set[str] = set()
        self._retry_after: dict[str, float] = {}
        self._captured = 0
        self._failures = 0
        self._suppressed = 0

    async def start(self) -> None:
        self._running = True
        self._bus.subscribe("event.received", self._on_event)

    async def stop(self) -> None:
        self._running = False
        self._bus.unsubscribe("event.received", self._on_event)
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _on_event(self, message: Message) -> None:
        event = message.data.get("event", {})
        if message.data.get("canonical_event_created") is False:
            return
        if event.get("event_state", "active") != "active":
            return
        device_id = event.get("device_id", "")
        if not event.get("episode_id") or not self._media.get(device_id):
            return
        if device_id in self._capturing or monotonic() < self._retry_after.get(device_id, 0):
            self._suppressed += 1
            return
        self._capturing.add(device_id)
        task = asyncio.create_task(self._capture(event))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _capture(self, event: dict) -> None:
        device_id = event["device_id"]
        try:
            data, content_type = await self._media.fetch_snapshot(device_id)
            extension = ".png" if content_type == "image/png" else ".jpg"
            path = await asyncio.to_thread(
                save_bytes,
                self._config.orphans_dir,
                "snapshots",
                data,
                prefix=f"onvif_{device_id[:12]}",
                extension=extension,
            )
            artifact = await asyncio.to_thread(
                describe_artifact,
                path,
                "snapshot",
                content_type,
                metadata={"origin": "onvif:snapshot"},
            )
            evidence = Evidence(
                device_id=device_id,
                area_id=event.get("area_id", ""),
                timestamp=datetime.now(timezone.utc),
                evidence_type="snapshot",
                file_path=path,
                mime_type=content_type,
                artifact_id=artifact.id,
                byte_size=artifact.byte_size,
                sha256=artifact.sha256,
                event_id=event.get("id"),
                episode_id=event.get("episode_id"),
                metadata={"origin": "onvif:snapshot", "requested_for": event.get("id")},
            )
            receipt = IngestionReceipt(
                source="onvif:snapshot",
                observed_at=evidence.timestamp,
                artifact_id=artifact.id,
                device_id=device_id,
                area_id=evidence.area_id,
                evidence_id=evidence.id,
                episode_id=evidence.episode_id,
                metadata={"action": "snapshot_capture"},
            )
            await self._bus.publish(
                Message(
                    type="evidence.received",
                    data={
                        "artifact": asdict(artifact),
                        "receipt": asdict(receipt),
                        "evidence": asdict(evidence),
                    },
                )
            )
            self._captured += 1
            self._retry_after.pop(device_id, None)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._failures += 1
            self._retry_after[device_id] = monotonic() + 30
            logger.warning("Snapshot capture failed for device %s: %s", device_id, error)
        finally:
            self._capturing.discard(device_id)

    def status(self) -> dict:
        return {
            "running": self._running,
            "captured": self._captured,
            "failures": self._failures,
            "suppressed": self._suppressed,
            "active": len(self._tasks),
        }
