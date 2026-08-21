from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import asdict
from datetime import datetime, timezone
from time import monotonic

from episode.domain.models import Event, Evidence, IngestionReceipt
from episode.engine.bus import EventBus, Message
from episode.engine.engine import CanonicalEventResult
from episode.media.registry import MediaRegistry
from episode.storage.files import describe_artifact, save_bytes

logger = logging.getLogger(__name__)


class SnapshotEngine:
    """Vendor-neutral snapshot action backed by discovered camera media."""

    def __init__(self, bus: EventBus, media: MediaRegistry, data_dir: str):
        self._bus = bus
        self._media = media
        self._orphans_dir = os.path.join(data_dir, "orphans")
        self._running = False
        self._tasks: set[asyncio.Task] = set()
        self._capturing: set[str] = set()
        self._retry_after: dict[str, float] = {}
        self._captured = 0
        self._failures = 0
        self._suppressed = 0

    async def start(self) -> None:
        self._running = True
        self._bus.subscribe("event.canonicalized", self._on_event)

    async def stop(self) -> None:
        self._running = False
        self._bus.unsubscribe("event.canonicalized", self._on_event)
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _on_event(self, message: Message) -> None:
        result = message.data.get("result")
        if not isinstance(result, CanonicalEventResult) or not result.created:
            return
        event = result.event
        if event.event_state.value != "active":
            return
        device_id = event.device_id
        if not event.episode_id or not self._media.get(device_id):
            return
        if device_id in self._capturing or monotonic() < self._retry_after.get(device_id, 0):
            self._suppressed += 1
            return
        self._capturing.add(device_id)
        task = asyncio.create_task(self._capture(event))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _capture(self, event: Event) -> None:
        device_id = event.device_id
        try:
            media = self._media.get(device_id)
            provider = re.sub(r"[^a-z0-9_-]+", "-", media.source.lower()) if media else "media"
            origin = f"{provider or 'media'}:snapshot"
            data, content_type = await self._media.fetch_snapshot(device_id)
            extension = ".png" if content_type == "image/png" else ".jpg"
            path = await asyncio.to_thread(
                save_bytes,
                self._orphans_dir,
                "snapshots",
                data,
                prefix=f"{provider or 'media'}_{device_id[:12]}",
                extension=extension,
            )
            artifact = await asyncio.to_thread(
                describe_artifact,
                path,
                "snapshot",
                content_type,
                metadata={"origin": origin},
            )
            evidence = Evidence(
                device_id=device_id,
                area_id=event.area_id,
                timestamp=datetime.now(timezone.utc),
                evidence_type="snapshot",
                file_path=path,
                mime_type=content_type,
                artifact_id=artifact.id,
                byte_size=artifact.byte_size,
                sha256=artifact.sha256,
                event_id=event.id,
                episode_id=event.episode_id,
                metadata={"origin": origin, "requested_for": event.id},
            )
            receipt = IngestionReceipt(
                source=origin,
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
