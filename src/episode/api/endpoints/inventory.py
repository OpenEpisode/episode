from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response

from episode.api.context import ApiContext
from episode.api.errors import PUBLIC_ERROR_RESPONSES
from episode.api.inventory import (
    AreaCreateRequest,
    AreaUpdateRequest,
    DeviceValidationResponse,
    DeviceWriteRequest,
    device_from_request,
    editable_device_configuration,
    validation_device_from_request,
)
from episode.api.runtime import product_capabilities
from episode.api.schemas import AreaResponse, DeviceDetailResponse, DeviceSummaryResponse
from episode.domain.models import Area
from episode.inventory import InventoryConflictError, InventoryService, stored_support


def inventory_router(context: ApiContext) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1",
        tags=["inventory"],
        responses=PUBLIC_ERROR_RESPONSES,
    )
    repo = context.repository

    def inventory_service() -> InventoryService:
        if not context.inventory:
            raise HTTPException(503, "Inventory management is unavailable")
        return context.inventory

    async def public_area(area) -> dict:
        devices = await repo.list_devices(area.id, include_disabled=True)
        return {
            "id": area.id,
            "name": area.name,
            "location": area.location,
            "enabled": area.enabled,
            "device_count": len(devices),
        }

    def device_summary(device):
        if context.operations:
            return context.operations.device_summary(device)
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
        if context.operations:
            result = context.operations.device_detail(device)
        else:
            result = {
                **device_summary(device),
                "ip_address": device.ip_address,
                "configuration": editable_device_configuration(device),
                "capture_policy": {
                    "recording": "unavailable",
                    "automatic_snapshots": False,
                    "onvif_events": None,
                    "activity_window_seconds": device.activity_window_seconds or 30,
                },
            }
        support = stored_support(device)
        device_integrations = [
            integration
            for integration in result.get("integrations", [])
            if integration.get("kind") == "device"
        ]
        integration_types = set(context.validator.integration_types if context.validator else ())
        integration_types.update(integration.get("type") for integration in device_integrations)
        integration_types.discard(None)

        for integration in device_integrations:
            integration_type = integration.get("type")
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
        for integration_type in sorted(integration_types):
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

    @router.get("/areas", response_model=list[AreaResponse])
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

    @router.post("/areas", response_model=AreaResponse, status_code=201)
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

    @router.get("/areas/{area_id}", response_model=AreaResponse)
    async def get_area(area_id: str):
        area = await repo.get_area(area_id)
        if not area:
            raise HTTPException(404, "Area not found")
        return await public_area(area)

    @router.put("/areas/{area_id}", response_model=AreaResponse)
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

    @router.delete("/areas/{area_id}", status_code=204)
    async def delete_area(area_id: str):
        try:
            await inventory_service().delete_area(area_id)
        except KeyError as exc:
            raise HTTPException(404, "Area not found") from exc
        except InventoryConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        return Response(status_code=204)

    @router.get("/devices", response_model=list[DeviceSummaryResponse])
    async def list_devices(area_id: str | None = None, include_disabled: bool = False):
        devices = await repo.list_devices(area_id, include_disabled=include_disabled)
        return [device_summary(device) for device in devices]

    @router.post("/devices/validate", response_model=DeviceValidationResponse)
    async def validate_device(payload: DeviceWriteRequest):
        if not context.validator:
            raise HTTPException(503, "Device validation is unavailable")
        existing = await repo.get_device(payload.id) if payload.id else None
        candidate = validation_device_from_request(payload, existing)
        results = await context.validator.validate(candidate)
        if existing:
            existing.metadata["integration_support"] = results
            await repo.upsert_device(existing)
        return {"device_id": existing.id if existing else None, "results": results}

    @router.post("/devices", response_model=DeviceDetailResponse, status_code=201)
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

    @router.get("/devices/{device_id}", response_model=DeviceDetailResponse)
    async def get_device(device_id: str):
        device = await repo.get_device(device_id)
        if not device:
            raise HTTPException(404, "Device not found")
        return await device_detail(device)

    @router.put("/devices/{device_id}", response_model=DeviceDetailResponse)
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

    @router.delete("/devices/{device_id}", status_code=204)
    async def delete_device(device_id: str):
        try:
            await inventory_service().delete_device(device_id)
        except KeyError as exc:
            raise HTTPException(404, "Device not found") from exc
        except InventoryConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        return Response(status_code=204)

    return router
