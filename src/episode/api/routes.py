from __future__ import annotations

import asyncio
import mimetypes
import os
from dataclasses import asdict

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

from episode import __version__
from episode.api.schemas import (
    AreaResponse,
    ClosestEventResponse,
    ClosestSnapshotResponse,
    DeviceResponse,
    EpisodeResponse,
    EventResponse,
    EvidenceResponse,
    IngestionReceiptResponse,
)
from episode.domain.models import EpisodeState
from episode.media.timelapse import (
    TimelapseGenerationError,
    TimelapseNotFoundError,
    TimelapseService,
)


def _public_event(event, receipt_sources: list[str] | None = None) -> EventResponse:
    data = asdict(event) if not isinstance(event, dict) else dict(event)
    source = data.pop("source", None)
    sources = list(data.pop("sources", []))
    for candidate in [source, *(receipt_sources or [])]:
        if candidate and candidate not in sources:
            sources.append(candidate)
    data["sources"] = sources
    data["has_raw_payload"] = bool(data.pop("raw_payload_path", None))
    return EventResponse.model_validate(data)


def _public_receipt(receipt) -> IngestionReceiptResponse:
    data = asdict(receipt) if not isinstance(receipt, dict) else dict(receipt)
    data["has_artifact"] = bool(data.get("artifact_id"))
    return IngestionReceiptResponse.model_validate(data)


def _public_evidence(evidence) -> EvidenceResponse:
    data = asdict(evidence) if not isinstance(evidence, dict) else dict(evidence)
    data.pop("file_path", None)
    return EvidenceResponse.model_validate(data)


def _event_annotations(event) -> tuple[dict[str, float] | None, str | None]:
    metadata = event.get("metadata", {}) if isinstance(event, dict) else event.metadata
    if not isinstance(metadata, dict):
        return None, None
    bounding_box = metadata.get("bounding_box")
    target_type = metadata.get("target_type")
    return (
        bounding_box if isinstance(bounding_box, dict) else None,
        target_type if isinstance(target_type, str) else None,
    )


def create_api(
    repo,
    data_dir: str = "",
    snapshot_window: int = 1,
    timelapses: TimelapseService | None = None,
) -> FastAPI:
    app = FastAPI(
        title="Episode",
        description="Local-first, event-driven incident capture API",
        version=__version__,
    )

    timelapse_service = timelapses or TimelapseService(repo, data_dir)

    async def public_event_with_receipts(event) -> EventResponse:
        event_id = event.get("id") if isinstance(event, dict) else event.id
        receipts = await repo.list_ingestion_receipts(event_id=event_id)
        return _public_event(event, [receipt.source for receipt in receipts])

    async def public_events(events) -> list[EventResponse]:
        return await asyncio.gather(*(public_event_with_receipts(event) for event in events))

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        return response

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": __version__}

    # --- Areas ---

    @app.get("/api/v1/areas", response_model=list[AreaResponse])
    async def list_areas():
        return await repo.list_areas()

    @app.get("/api/v1/areas/{area_id}", response_model=AreaResponse)
    async def get_area(area_id: str):
        area = await repo.get_area(area_id)
        if not area:
            raise HTTPException(404, "Area not found")
        return area

    # --- Devices ---

    @app.get("/api/v1/devices", response_model=list[DeviceResponse])
    async def list_devices(area_id: str | None = None):
        return await repo.list_devices(area_id)

    @app.get("/api/v1/devices/{device_id}", response_model=DeviceResponse)
    async def get_device(device_id: str):
        device = await repo.get_device(device_id)
        if not device:
            raise HTTPException(404, "Device not found")
        return device

    # --- Episodes ---

    @app.get("/api/v1/episodes", response_model=list[EpisodeResponse])
    async def list_episodes(
        area_id: str | None = None,
        state: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        s = EpisodeState(state) if state else None
        return await repo.list_episodes(area_id, s, limit, offset)

    @app.get("/api/v1/episodes/{episode_id}", response_model=EpisodeResponse)
    async def get_episode(episode_id: str):
        episode = await repo.get_episode(episode_id)
        if not episode:
            raise HTTPException(404, "Episode not found")
        return episode

    @app.get("/api/v1/episodes/{episode_id}/events")
    async def episode_events(episode_id: str):
        episode = await repo.get_episode(episode_id)
        if not episode:
            raise HTTPException(404, "Episode not found")
        events = await repo.list_events(
            episode_id=episode_id, limit=max(episode.event_count, 10000)
        )
        return await public_events(events)

    @app.get("/api/v1/episodes/{episode_id}/evidence", response_model=list[EvidenceResponse])
    async def episode_evidence(episode_id: str):
        episode = await repo.get_episode(episode_id)
        if not episode:
            raise HTTPException(404, "Episode not found")
        evidence = await repo.list_evidence(
            episode_id=episode_id,
            limit=max(episode.evidence_count, 10000),
        )
        return [_public_evidence(item) for item in evidence]

    @app.get(
        "/api/v1/episodes/{episode_id}/receipts",
        response_model=list[IngestionReceiptResponse],
    )
    async def episode_receipts(episode_id: str):
        if not await repo.get_episode(episode_id):
            raise HTTPException(404, "Episode not found")
        receipts = await repo.list_ingestion_receipts(episode_id=episode_id, limit=10000)
        return [_public_receipt(receipt) for receipt in receipts]

    @app.get("/api/v1/episodes/{episode_id}/timelapse")
    async def episode_timelapse(episode_id: str, device_id: str | None = None):
        try:
            path = await timelapse_service.get_or_create(episode_id, device_id)
        except TimelapseNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except TimelapseGenerationError as exc:
            raise HTTPException(500, str(exc)) from exc
        return FileResponse(
            path,
            media_type="video/mp4",
            headers={
                "Content-Disposition": f'inline; filename="{os.path.basename(path)}"',
            },
        )

    @app.get("/api/v1/events", response_model=list[EventResponse])
    async def list_events(
        episode_id: str | None = None,
        area_id: str | None = None,
        device_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ):
        events = await repo.list_events(episode_id, area_id, device_id, limit, offset)
        return await public_events(events)

    @app.get("/api/v1/events/{event_id}", response_model=EventResponse)
    async def get_event(event_id: str):
        event = await repo.get_event(event_id)
        if not event:
            raise HTTPException(404, "Event not found")
        return await public_event_with_receipts(event)

    @app.get("/api/v1/events/{event_id}/closest-snapshot", response_model=ClosestSnapshotResponse)
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
            e
            for e in evidence
            if e.evidence_type == "snapshot"
            and e.file_path
            and os.path.exists(e.file_path)
            and e.timestamp >= event.timestamp
        ]
        if not snapshots:
            raise HTTPException(404, "No snapshots found for this event")

        evt_ts = event.timestamp
        closest = min(
            snapshots,
            key=lambda e: abs(e.timestamp - evt_ts),
        )
        if snapshot_window and abs((closest.timestamp - evt_ts).total_seconds()) > snapshot_window:
            raise HTTPException(404, "Closest snapshot exceeds snapshot window")

        bbox, target_type = _event_annotations(event)

        return {
            "snapshot": _public_evidence(closest),
            "bounding_box": bbox,
            "target_type": target_type,
        }

    @app.get("/api/v1/events/{event_id}/payload")
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

    # --- Ingestion receipts ---

    @app.get("/api/v1/receipts", response_model=list[IngestionReceiptResponse])
    async def list_receipts(
        episode_id: str | None = None,
        event_id: str | None = None,
        evidence_id: str | None = None,
        limit: int = 200,
    ):
        receipts = await repo.list_ingestion_receipts(
            episode_id=episode_id,
            event_id=event_id,
            evidence_id=evidence_id,
            limit=limit,
        )
        return [_public_receipt(receipt) for receipt in receipts]

    @app.get("/api/v1/receipts/{receipt_id}/artifact")
    async def receipt_artifact(receipt_id: str):
        receipt = await repo.get_ingestion_receipt(receipt_id)
        if not receipt or not receipt.artifact_id:
            raise HTTPException(404, "Receipt artifact not found")
        artifact = await repo.get_raw_artifact(receipt.artifact_id)
        if not artifact or not os.path.isfile(artifact.file_path):
            raise HTTPException(404, "Receipt artifact not found")
        return FileResponse(
            artifact.file_path,
            media_type=artifact.mime_type,
            filename=artifact.original_filename or os.path.basename(artifact.file_path),
        )

    # --- Evidence ---

    @app.get("/api/v1/evidence", response_model=list[EvidenceResponse])
    async def list_evidence(
        episode_id: str | None = None,
        event_id: str | None = None,
        device_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ):
        evidence = await repo.list_evidence(episode_id, event_id, device_id, limit, offset)
        return [_public_evidence(item) for item in evidence]

    @app.get("/api/v1/covers")
    async def covers(ids: str = ""):
        if not ids:
            return {}
        ep_ids = [x.strip() for x in ids.split(",") if x.strip()]
        if not ep_ids:
            return {}
        return await repo.episode_covers(ep_ids)

    @app.get("/api/v1/evidence/{evidence_id}", response_model=EvidenceResponse)
    async def get_evidence(evidence_id: str):
        evidence = await repo.get_evidence(evidence_id)
        if not evidence:
            raise HTTPException(404, "Evidence not found")
        return _public_evidence(evidence)

    @app.get("/api/v1/evidence/{evidence_id}/closest-event", response_model=ClosestEventResponse)
    async def evidence_closest_event(evidence_id: str):
        ev = await repo.get_evidence(evidence_id)
        if not ev:
            raise HTTPException(404, "Evidence not found")
        if not ev.episode_id:
            raise HTTPException(404, "Evidence not linked to an episode")

        events = await repo.list_events(
            episode_id=ev.episode_id,
            device_id=ev.device_id,
        )
        events = [event for event in events if event.timestamp <= ev.timestamp]
        if not events:
            raise HTTPException(404, "No events found for this evidence")

        closest = min(events, key=lambda event: abs(event.timestamp - ev.timestamp))
        if (
            snapshot_window
            and abs((closest.timestamp - ev.timestamp).total_seconds()) > snapshot_window
        ):
            raise HTTPException(404, "Closest event exceeds snapshot window")

        bbox, target_type = _event_annotations(closest)

        return {
            "event": await public_event_with_receipts(closest),
            "bounding_box": bbox,
            "target_type": target_type,
        }

    @app.get("/api/v1/evidence/{evidence_id}/file")
    async def serve_evidence_file(evidence_id: str):
        evidence = await repo.get_evidence(evidence_id)
        if not evidence:
            raise HTTPException(404, "Evidence not found")
        if not os.path.exists(evidence.file_path):
            raise HTTPException(404, "File not found on disk")
        return FileResponse(evidence.file_path, media_type=evidence.mime_type)

    return app
