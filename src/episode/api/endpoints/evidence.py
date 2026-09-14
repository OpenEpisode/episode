from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, RedirectResponse

from episode.api.context import ApiContext
from episode.api.errors import PUBLIC_ERROR_RESPONSES
from episode.api.pagination import DEFAULT_LIMIT, PageLimit, PageOffset
from episode.api.projections import event_annotations, public_evidence
from episode.api.schemas import ClosestEventResponse, EvidenceResponse
from episode.api.time_ranges import normalize_datetime_range
from episode.recording.hls import HLSRecordingBundle


def _component_media_type(path: Path) -> str:
    return {
        ".m3u8": "application/vnd.apple.mpegurl",
        ".m4s": "video/iso.segment",
        ".mp4": "video/mp4",
        ".json": "application/json",
    }.get(path.suffix.lower(), "application/octet-stream")


def evidence_router(context: ApiContext) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1",
        tags=["evidence"],
        responses=PUBLIC_ERROR_RESPONSES,
    )
    repo = context.repository

    @router.get("/evidence", response_model=list[EvidenceResponse])
    async def list_evidence(
        episode_id: str | None = None,
        event_id: str | None = None,
        device_id: str | None = None,
        area_id: str | None = None,
        evidence_type: str | None = None,
        has_episode: bool | None = None,
        captured_from: datetime | None = None,
        captured_before: datetime | None = None,
        limit: PageLimit = DEFAULT_LIMIT,
        offset: PageOffset = 0,
    ):
        captured_from, captured_before = normalize_datetime_range(
            captured_from,
            captured_before,
            start_name="captured_from",
            end_name="captured_before",
        )
        evidence = await repo.list_evidence(
            episode_id,
            event_id,
            device_id,
            limit,
            offset,
            area_id=area_id,
            evidence_type=evidence_type,
            has_episode=has_episode,
            captured_from=captured_from,
            captured_before=captured_before,
        )
        return [public_evidence(item) for item in evidence]

    @router.get("/covers", response_model=dict[str, str])
    async def covers(ids: str = ""):
        if not ids:
            return {}
        episode_ids = [value.strip() for value in ids.split(",") if value.strip()]
        if not episode_ids:
            return {}
        return await repo.episode_covers(episode_ids)

    @router.get("/evidence/{evidence_id}", response_model=EvidenceResponse)
    async def get_evidence(evidence_id: str):
        evidence = await repo.get_evidence(evidence_id)
        if not evidence:
            raise HTTPException(404, "Evidence not found")
        return public_evidence(evidence)

    @router.get("/evidence/{evidence_id}/closest-event", response_model=ClosestEventResponse)
    async def evidence_closest_event(evidence_id: str):
        evidence = await repo.get_evidence(evidence_id)
        if not evidence:
            raise HTTPException(404, "Evidence not found")
        if evidence.availability == "expired":
            return {"event": None, "bounding_box": None, "target_type": None}
        no_match = {"event": None, "bounding_box": None, "target_type": None}
        if not evidence.episode_id:
            return no_match

        events = await repo.list_events(
            episode_id=evidence.episode_id,
            device_id=evidence.device_id,
        )
        events = [event for event in events if event.timestamp <= evidence.timestamp]
        if not events:
            return no_match

        closest = min(events, key=lambda event: abs(event.timestamp - evidence.timestamp))
        if (
            context.snapshot_window
            and abs((closest.timestamp - evidence.timestamp).total_seconds())
            > context.snapshot_window
        ):
            return no_match

        bounding_box, target_type = event_annotations(closest)
        return {
            "event": await context.public_event(closest),
            "bounding_box": bounding_box,
            "target_type": target_type,
        }

    @router.get("/evidence/{evidence_id}/file")
    async def serve_evidence_file(evidence_id: str):
        evidence = await repo.get_evidence(evidence_id)
        if not evidence:
            raise HTTPException(404, "Evidence not found")
        if evidence.availability == "expired":
            raise HTTPException(410, "Evidence expired under the retention policy")
        if not os.path.exists(evidence.file_path):
            raise HTTPException(404, "File not found on disk")
        if evidence.metadata.get("format") == "hls-fmp4":
            return RedirectResponse(
                f"/api/v1/recordings/{evidence.id}/index.m3u8",
                status_code=307,
            )
        return FileResponse(evidence.file_path, media_type=evidence.mime_type)

    @router.get("/recordings/{evidence_id}/{component_path:path}")
    async def serve_recording_component(evidence_id: str, component_path: str):
        allowed = component_path in {"index.m3u8", "init.mp4", "manifest.json"} or (
            component_path.startswith("segments/") and component_path.endswith(".m4s")
        )
        if not allowed:
            raise HTTPException(404, "Recording component not found")
        bundle = context.recorder.active_bundle(evidence_id) if context.recorder else None
        active = bundle is not None
        if bundle is None:
            evidence = await repo.get_evidence(evidence_id)
            if not evidence:
                raise HTTPException(404, "Recording not found")
            if evidence.availability == "expired":
                raise HTTPException(410, "Recording expired under the retention policy")
            if evidence.metadata.get("format") != "hls-fmp4":
                raise HTTPException(404, "Recording is not an HLS bundle")
            try:
                bundle = HLSRecordingBundle.load_from_evidence(Path(evidence.file_path), evidence)
            except (KeyError, TypeError, ValueError) as error:
                raise HTTPException(404, "Recording bundle metadata is incomplete") from error
        path = bundle.resolve_component(component_path)
        if path is None:
            raise HTTPException(404, "Recording component not found")
        headers = (
            {"Cache-Control": "no-store"}
            if active or path.suffix == ".m3u8"
            else {"Cache-Control": "private, max-age=31536000, immutable"}
        )
        return FileResponse(path, media_type=_component_media_type(path), headers=headers)

    @router.get("/evidence/{evidence_id}/thumbnail")
    async def serve_evidence_thumbnail(evidence_id: str):
        evidence = await repo.get_evidence(evidence_id)
        if not evidence:
            raise HTTPException(404, "Evidence not found")
        if evidence.availability == "expired":
            raise HTTPException(410, "Evidence expired under the retention policy")
        if not context.thumbnails:
            raise HTTPException(404, "Thumbnail not available")

        thumbnail_path = await context.thumbnails.get_or_create(evidence)
        if not thumbnail_path:
            raise HTTPException(404, "Thumbnail not available")
        return FileResponse(
            thumbnail_path,
            media_type="image/jpeg",
            headers={"Cache-Control": "private, max-age=86400"},
        )

    return router
