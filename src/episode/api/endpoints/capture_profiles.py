from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response

from episode.api.context import ApiContext
from episode.api.errors import PUBLIC_ERROR_RESPONSES
from episode.api.pagination import PageLimit
from episode.api.projections import public_capture_profile, public_capture_profile_change
from episode.api.schemas import (
    ActiveCaptureProfileResponse,
    CaptureProfileActivationRequest,
    CaptureProfileCreateRequest,
    CaptureProfileResponse,
    CaptureProfileUpdateRequest,
)
from episode.capture_profiles import (
    CaptureProfileConflictError,
    CaptureProfileError,
    CaptureProfileNotFoundError,
    CaptureProfileService,
)


def capture_profiles_router(context: ApiContext) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/capture-profiles",
        tags=["capture-profiles"],
        responses=PUBLIC_ERROR_RESPONSES,
    )

    def service() -> CaptureProfileService:
        if not context.capture_profiles:
            raise HTTPException(503, "Capture profile service is unavailable")
        return context.capture_profiles

    @router.get("", response_model=list[CaptureProfileResponse])
    async def list_capture_profiles():
        return [public_capture_profile(profile) for profile in await service().list_profiles()]

    @router.post("", response_model=CaptureProfileResponse, status_code=201)
    async def create_capture_profile(payload: CaptureProfileCreateRequest):
        try:
            profile = await service().create_profile(
                payload.name,
                payload.device_ids,
                event_filter=payload.event_filter,
            )
        except CaptureProfileConflictError as error:
            raise HTTPException(409, str(error)) from error
        except CaptureProfileError as error:
            raise HTTPException(422, str(error)) from error
        return public_capture_profile(profile)

    # Keep the static ``active`` path before ``/{profile_id}``; otherwise
    # Starlette may parse the word active as a profile identifier.
    @router.get("/active", response_model=ActiveCaptureProfileResponse)
    async def active_capture_profile(limit: PageLimit = 20):
        try:
            profile, changes = await service().active_response(limit=limit)
        except CaptureProfileError as error:
            raise HTTPException(422, str(error)) from error
        return {
            "profile": public_capture_profile(profile),
            "recent_changes": [public_capture_profile_change(item) for item in changes],
        }

    @router.put("/active", response_model=ActiveCaptureProfileResponse)
    async def activate_capture_profile(payload: CaptureProfileActivationRequest):
        try:
            profile, _change, changes = await service().activate_profile(
                payload.profile_id,
                source="api",
            )
        except CaptureProfileNotFoundError as error:
            raise HTTPException(404, str(error)) from error
        except CaptureProfileConflictError as error:
            raise HTTPException(409, str(error)) from error
        except CaptureProfileError as error:
            raise HTTPException(422, str(error)) from error
        return {
            "profile": public_capture_profile(profile),
            "recent_changes": [public_capture_profile_change(item) for item in changes],
        }

    @router.get("/{profile_id}", response_model=CaptureProfileResponse)
    async def get_capture_profile(profile_id: str):
        profile = await service().get_profile(profile_id)
        if profile is None:
            raise HTTPException(404, "Capture profile not found")
        return public_capture_profile(profile)

    @router.put("/{profile_id}", response_model=CaptureProfileResponse)
    async def update_capture_profile(
        profile_id: str,
        payload: CaptureProfileUpdateRequest,
    ):
        try:
            profile = await service().update_profile(
                profile_id,
                payload.name,
                payload.device_ids,
                event_filter=payload.event_filter,
            )
        except CaptureProfileNotFoundError as error:
            raise HTTPException(404, str(error)) from error
        except CaptureProfileConflictError as error:
            raise HTTPException(409, str(error)) from error
        except CaptureProfileError as error:
            raise HTTPException(422, str(error)) from error
        return public_capture_profile(profile)

    @router.delete("/{profile_id}", status_code=204)
    async def delete_capture_profile(profile_id: str):
        try:
            await service().delete_profile(profile_id)
        except CaptureProfileNotFoundError as error:
            raise HTTPException(404, str(error)) from error
        except CaptureProfileConflictError as error:
            raise HTTPException(409, str(error)) from error
        except CaptureProfileError as error:
            raise HTTPException(422, str(error)) from error
        return Response(status_code=204)

    return router
