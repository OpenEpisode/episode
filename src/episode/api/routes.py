from __future__ import annotations

import asyncio
import mimetypes
import os
from dataclasses import asdict

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse

from episode import __version__
from episode.api.inventory import (
    AreaCreateRequest,
    AreaUpdateRequest,
    DeviceValidationResponse,
    DeviceWriteRequest,
    device_from_request,
    editable_device_configuration,
    validation_device_from_request,
)
from episode.api.runtime import (
    DeviceDetailResponse,
    DeviceSummaryResponse,
    DiagnosticsResponse,
    OperationalView,
    SystemStatusResponse,
    product_capabilities,
)
from episode.api.schemas import (
    AreaResponse,
    ClosestEventResponse,
    ClosestSnapshotResponse,
    EpisodeResponse,
    EventResponse,
    EvidenceResponse,
    IngestionReceiptResponse,
)
from episode.domain.models import Area, EpisodeState
from episode.inventory import (
    DeviceValidationService,
    InventoryConflictError,
    InventoryService,
    stored_support,
)
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
    operations: OperationalView | None = None,
    inventory: InventoryService | None = None,
    validator: DeviceValidationService | None = None,
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

    @app.get("/api/v1/status", response_model=SystemStatusResponse)
    async def system_status():
        if operations:
            return operations.status()
        return {
            "version": __version__,
            "state": "unknown",
            "active_recordings": 0,
            "services": {
                "engine": "unknown",
                "recorder": "unknown",
                "snapshots": "unknown",
            },
            "integrations": {
                "total": 0,
                "healthy": 0,
                "degraded": 0,
                "unavailable": 0,
            },
        }

    @app.get("/api/v1/diagnostics", response_model=DiagnosticsResponse)
    async def diagnostics():
        if operations:
            return operations.diagnostics()
        return {"status": await system_status(), "services": [], "integrations": []}

    # --- Areas and Devices ---

    def inventory_service() -> InventoryService:
        if not inventory:
            raise HTTPException(503, "Inventory management is unavailable")
        return inventory

    async def public_area(area) -> dict:
        devices = await repo.list_devices(area.id, include_disabled=True)
        return {
            "id": area.id,
            "name": area.name,
            "location": area.location,
            "enabled": area.enabled,
            "device_count": len(devices),
        }

    @app.get("/api/v1/areas", response_model=list[AreaResponse])
    async def list_areas(include_disabled: bool = False):
        areas = await repo.list_areas(include_disabled=include_disabled)
        devices = await repo.list_devices(include_disabled=True)
        counts = {area.id: 0 for area in areas}
        for device in devices:
            if device.area_id in counts:
                counts[device.area_id] += 1
        return [
            {
                "id": area.id,
                "name": area.name,
                "location": area.location,
                "enabled": area.enabled,
                "device_count": counts[area.id],
            }
            for area in areas
        ]

    @app.post("/api/v1/areas", response_model=AreaResponse, status_code=201)
    async def create_area(payload: AreaCreateRequest):
        service = inventory_service()
        area_id = payload.id or await service.available_area_id(payload.name)
        try:
            area = await service.save_area(
                Area(id=area_id, name=payload.name, location=payload.location),
                create=True,
            )
        except InventoryConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        return await public_area(area)

    @app.get("/api/v1/areas/{area_id}", response_model=AreaResponse)
    async def get_area(area_id: str):
        area = await repo.get_area(area_id)
        if not area:
            raise HTTPException(404, "Area not found")
        return await public_area(area)

    @app.put("/api/v1/areas/{area_id}", response_model=AreaResponse)
    async def update_area(area_id: str, payload: AreaUpdateRequest):
        existing = await repo.get_area(area_id)
        if not existing:
            raise HTTPException(404, "Area not found")
        try:
            area = await inventory_service().save_area(
                Area(
                    id=area_id,
                    name=payload.name,
                    location=payload.location,
                    metadata=existing.metadata,
                    enabled=payload.enabled,
                ),
                create=False,
            )
        except InventoryConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        return await public_area(area)

    @app.delete("/api/v1/areas/{area_id}", status_code=204)
    async def delete_area(area_id: str):
        try:
            await inventory_service().delete_area(area_id)
        except KeyError as exc:
            raise HTTPException(404, "Area not found") from exc
        except InventoryConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        return Response(status_code=204)

    def device_summary(device):
        if operations:
            return operations.device_summary(device)
        metadata = device.metadata.get("onvif", {})
        return {
            "id": device.id,
            "name": device.name,
            "device_type": device.device_type,
            "area_id": device.area_id,
            "capabilities": product_capabilities(device.capabilities),
            "state": "disabled" if not device.enabled else "unknown",
            "identity": {
                "manufacturer": metadata.get("manufacturer"),
                "model": metadata.get("model"),
                "firmware_version": metadata.get("firmware_version"),
            },
            "enabled": device.enabled,
            "integrations": [],
        }

    async def device_detail(device) -> dict:
        if operations:
            result = operations.device_detail(device)
        else:
            result = {
                **device_summary(device),
                "ip_address": device.ip_address,
                "configuration": editable_device_configuration(device),
                "capture_policy": {
                    "recording": "unavailable",
                    "automatic_snapshots": False,
                    "onvif_events": None,
                },
            }
        support = stored_support(device)
        for integration in result.get("integrations", []):
            integration_type = integration.get("type")
            if integration_type not in {"onvif", "isapi", "hikvision_sdk"}:
                continue
            if integration.get("state") == "healthy":
                previous = support.get(integration_type, {})
                support[integration_type] = {
                    "status": "supported",
                    "summary": f"{integration.get('name', integration_type)} is connected",
                    "checked_at": previous.get("checked_at"),
                    "capabilities": integration.get("capabilities", []),
                    "details": previous.get("details", {}),
                }
            elif integration_type not in support:
                support[integration_type] = {
                    "status": "unavailable",
                    "summary": str(integration.get("summary") or "Configured but unavailable"),
                    "checked_at": None,
                    "capabilities": [],
                    "details": {},
                }
        for integration_type in ("onvif", "isapi", "hikvision_sdk"):
            support.setdefault(
                integration_type,
                {
                    "status": "not_validated",
                    "summary": "Support has not been validated",
                    "checked_at": None,
                    "capabilities": [],
                    "details": {},
                },
            )
        result["integration_support"] = support
        usage = await repo.device_usage(device.id)
        result["can_delete"] = not any(usage.values())
        return result

    @app.get("/api/v1/devices", response_model=list[DeviceSummaryResponse])
    async def list_devices(area_id: str | None = None, include_disabled: bool = False):
        devices = await repo.list_devices(area_id, include_disabled=include_disabled)
        return [device_summary(device) for device in devices]

    @app.post("/api/v1/devices/validate", response_model=DeviceValidationResponse)
    async def validate_device(payload: DeviceWriteRequest):
        if not validator:
            raise HTTPException(503, "Device validation is unavailable")
        existing = await repo.get_device(payload.id) if payload.id else None
        candidate = validation_device_from_request(payload, existing)
        results = await validator.validate(candidate)
        if existing:
            existing.metadata["integration_support"] = results
            await repo.upsert_device(existing)
        return {"device_id": existing.id if existing else None, "results": results}

    @app.post("/api/v1/devices", response_model=DeviceDetailResponse, status_code=201)
    async def create_device(payload: DeviceWriteRequest):
        service = inventory_service()
        device_id = payload.id or await service.available_device_id(payload.name)
        try:
            device = await service.save_device(
                device_from_request(device_id, payload),
                create=True,
            )
        except InventoryConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        return await device_detail(device)

    @app.get("/api/v1/devices/{device_id}", response_model=DeviceDetailResponse)
    async def get_device(device_id: str):
        device = await repo.get_device(device_id)
        if not device:
            raise HTTPException(404, "Device not found")
        return await device_detail(device)

    @app.put("/api/v1/devices/{device_id}", response_model=DeviceDetailResponse)
    async def update_device(device_id: str, payload: DeviceWriteRequest):
        existing = await repo.get_device(device_id)
        if not existing:
            raise HTTPException(404, "Device not found")
        if payload.id and payload.id != device_id:
            raise HTTPException(409, "Device IDs cannot be changed")
        try:
            device = await inventory_service().save_device(
                device_from_request(device_id, payload, existing),
                create=False,
            )
        except InventoryConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        return await device_detail(device)

    @app.delete("/api/v1/devices/{device_id}", status_code=204)
    async def delete_device(device_id: str):
        try:
            await inventory_service().delete_device(device_id)
        except KeyError as exc:
            raise HTTPException(404, "Device not found") from exc
        except InventoryConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        return Response(status_code=204)

    # --- Episodes ---

    @app.get("/api/v1/episodes", response_model=list[EpisodeResponse])
    async def list_episodes(
        area_id: str | None = None,
        state: EpisodeState | None = None,
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ):
        return await repo.list_episodes(area_id, state, limit, offset)

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
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
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
        limit: int = Query(200, ge=1, le=1000),
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
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
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
