from __future__ import annotations

import mimetypes
import os
from datetime import datetime

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from episode.api.context import ApiContext
from episode.api.errors import PUBLIC_ERROR_RESPONSES
from episode.api.pagination import DEFAULT_LIMIT, PageLimit, PageOffset
from episode.api.projections import event_annotations, event_embedded_picture, public_evidence
from episode.api.schemas import ClosestSnapshotResponse, EventResponse
from episode.api.time_ranges import normalize_datetime_range


def events_router(context: ApiContext) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/events",
        tags=["events"],
        responses=PUBLIC_ERROR_RESPONSES,
    )
    repo = context.repository

    @router.get("", response_model=list[EventResponse])
    async def list_events(
        episode_id: str | None = None,
        area_id: str | None = None,
        device_id: str | None = None,
        event_type: str | None = None,
        event_state: str | None = None,
        has_episode: bool | None = None,
        observed_from: datetime | None = None,
        observed_before: datetime | None = None,
        limit: PageLimit = DEFAULT_LIMIT,
        offset: PageOffset = 0,
    ):
        observed_from, observed_before = normalize_datetime_range(
            observed_from,
            observed_before,
            start_name="observed_from",
            end_name="observed_before",
        )
        events = await repo.list_events(
            episode_id,
            area_id,
            device_id,
            limit,
            offset,
            event_type=event_type,
            event_state=event_state,
            has_episode=has_episode,
            observed_from=observed_from,
            observed_before=observed_before,
        )
        return await context.public_events(events)

    @router.get("/{event_id}", response_model=EventResponse)
    async def get_event(event_id: str):
        event = await repo.get_event(event_id)
        if not event:
            raise HTTPException(404, "Event not found")
        return await context.public_event(event)

    @router.get("/{event_id}/closest-snapshot", response_model=ClosestSnapshotResponse)
    async def event_closest_snapshot(event_id: str):
        event = await repo.get_event(event_id)
        if not event:
            raise HTTPException(404, "Event not found")
        if not event.episode_id:
            raise HTTPException(404, "Event not linked to an episode")

        evidence = await repo.list_evidence(
            episode_id=event.episode_id,
            device_id=event.device_id,
        )
        snapshots = [
            item
            for item in evidence
            if item.evidence_type == "snapshot"
            and item.file_path
            and os.path.exists(item.file_path)
            and item.timestamp >= event.timestamp
        ]
        if not snapshots:
            raise HTTPException(404, "No snapshots found for this event")

        closest = min(snapshots, key=lambda item: abs(item.timestamp - event.timestamp))
        if (
            context.snapshot_window
            and abs((closest.timestamp - event.timestamp).total_seconds()) > context.snapshot_window
        ):
            raise HTTPException(404, "Closest snapshot exceeds snapshot window")

        bounding_box, target_type = event_annotations(event)
        return {
            "snapshot": public_evidence(closest),
            "bounding_box": bounding_box,
            "target_type": target_type,
        }

    @router.get("/{event_id}/payload")
    async def event_payload(event_id: str):
        event = await repo.get_event(event_id)
        if not event:
            raise HTTPException(404, "Event not found")
        if not event.raw_payload_path or not os.path.exists(event.raw_payload_path):
            raise HTTPException(404, "Payload not found")
        media_type = mimetypes.guess_type(event.raw_payload_path)[0] or "application/octet-stream"
        return FileResponse(
            event.raw_payload_path,
            media_type=media_type,
            filename=os.path.basename(event.raw_payload_path),
        )

    @router.get("/{event_id}/picture")
    async def event_picture(event_id: str):
        event = await repo.get_event(event_id)
        if not event:
            raise HTTPException(404, "Event not found")
        visual_evidence = event.metadata.get("visual_evidence")
        if isinstance(visual_evidence, dict) and visual_evidence.get("availability") == "expired":
            raise HTTPException(410, "Event picture expired under the retention policy")
        picture = event_embedded_picture(event)
        raw_path = event.raw_payload_path
        if not picture or not raw_path or not os.path.isfile(raw_path):
            raise HTTPException(404, "Event picture not found")

        offset = int(picture["offset"])
        byte_size = int(picture["byte_size"])
        if offset + byte_size > os.path.getsize(raw_path):
            raise HTTPException(404, "Event picture not found")

        def picture_bytes():
            remaining = byte_size
            with open(raw_path, "rb") as payload_file:
                payload_file.seek(offset)
                while remaining:
                    chunk = payload_file.read(min(remaining, 64 * 1024))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        headers = {
            "Content-Length": str(byte_size),
            "Content-Disposition": f'inline; filename="{picture["filename"]}"',
        }
        if picture["sha256"]:
            headers["ETag"] = f'"{picture["sha256"]}"'
        return StreamingResponse(
            picture_bytes(), media_type=str(picture["mime_type"]), headers=headers
        )

    return router
