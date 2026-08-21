from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import FileResponse

from episode.api.context import ApiContext
from episode.api.errors import PUBLIC_ERROR_RESPONSES
from episode.api.pagination import DEFAULT_LIMIT, PageLimit, PageOffset
from episode.api.projections import public_episode, public_evidence, public_receipt
from episode.api.schemas import (
    CurrentViewResponse,
    EpisodeResponse,
    EventResponse,
    EvidenceResponse,
    IngestionReceiptResponse,
)
from episode.domain.models import EpisodeState
from episode.media.timelapse import TimelapseGenerationError, TimelapseNotFoundError


def episodes_router(context: ApiContext) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/episodes",
        tags=["episodes"],
        responses=PUBLIC_ERROR_RESPONSES,
    )
    repo = context.repository

    @router.get("", response_model=list[EpisodeResponse])
    async def list_episodes(
        area_id: str | None = None,
        state: EpisodeState | None = None,
        limit: PageLimit = DEFAULT_LIMIT,
        offset: PageOffset = 0,
    ):
        episodes = await repo.list_episodes(area_id, state, limit, offset)
        trigger_types = await repo.episode_trigger_event_types([episode.id for episode in episodes])
        return [public_episode(episode, trigger_types.get(episode.id)) for episode in episodes]

    @router.get("/{episode_id}", response_model=EpisodeResponse)
    async def get_episode(episode_id: str):
        episode = await repo.get_episode(episode_id)
        if not episode:
            raise HTTPException(404, "Episode not found")
        trigger_types = await repo.episode_trigger_event_types([episode.id])
        return public_episode(episode, trigger_types.get(episode.id))

    @router.get("/{episode_id}/current-views", response_model=list[CurrentViewResponse])
    async def episode_current_views(episode_id: str):
        episode = await repo.get_episode(episode_id)
        if not episode:
            raise HTTPException(404, "Episode not found")
        if not context.current_views or episode.state == EpisodeState.CLOSED:
            return []

        result = []
        for view in context.current_views.describe(episode_id):
            device = await repo.get_device(view.device_id)
            available = view.mode == "snapshot"
            result.append(
                {
                    "device_id": view.device_id,
                    "device_name": device.name if device else view.device_id,
                    "mode": view.mode,
                    "refresh_interval_seconds": view.refresh_interval_seconds,
                    "image_url": (
                        f"/api/v1/episodes/{episode_id}/current-views/{view.device_id}"
                        if available
                        else None
                    ),
                    "summary": (
                        "Refreshing while this Device records"
                        if available
                        else "Recording continues without a preview provider"
                    ),
                }
            )
        return result

    @router.get("/{episode_id}/current-views/{device_id}")
    async def episode_current_view_image(episode_id: str, device_id: str):
        episode = await repo.get_episode(episode_id)
        if not episode:
            raise HTTPException(404, "Episode not found")
        if not context.current_views or episode.state == EpisodeState.CLOSED:
            raise HTTPException(404, "Current view is not available")
        try:
            content, media_type = await context.current_views.fetch(episode_id, device_id)
        except LookupError as error:
            raise HTTPException(404, str(error)) from error
        except Exception as error:
            raise HTTPException(503, "Current view is temporarily unavailable") from error
        return Response(
            content=content,
            media_type=media_type,
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/{episode_id}/events", response_model=list[EventResponse])
    async def episode_events(
        episode_id: str,
        limit: PageLimit = DEFAULT_LIMIT,
        offset: PageOffset = 0,
    ):
        episode = await repo.get_episode(episode_id)
        if not episode:
            raise HTTPException(404, "Episode not found")
        events = await repo.list_events(episode_id=episode_id, limit=limit, offset=offset)
        return await context.public_events(events)

    @router.get("/{episode_id}/evidence", response_model=list[EvidenceResponse])
    async def episode_evidence(
        episode_id: str,
        limit: PageLimit = DEFAULT_LIMIT,
        offset: PageOffset = 0,
    ):
        episode = await repo.get_episode(episode_id)
        if not episode:
            raise HTTPException(404, "Episode not found")
        evidence = await repo.list_evidence(
            episode_id=episode_id,
            limit=limit,
            offset=offset,
        )
        return [public_evidence(item) for item in evidence]

    @router.get("/{episode_id}/receipts", response_model=list[IngestionReceiptResponse])
    async def episode_receipts(
        episode_id: str,
        limit: PageLimit = DEFAULT_LIMIT,
        offset: PageOffset = 0,
    ):
        if not await repo.get_episode(episode_id):
            raise HTTPException(404, "Episode not found")
        receipts = await repo.list_ingestion_receipts(
            episode_id=episode_id,
            limit=limit,
            offset=offset,
        )
        return [public_receipt(receipt) for receipt in receipts]

    @router.get("/{episode_id}/timelapse")
    async def episode_timelapse(episode_id: str, device_id: str | None = None):
        try:
            path = await context.timelapses.get_or_create(episode_id, device_id)
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

    return router
