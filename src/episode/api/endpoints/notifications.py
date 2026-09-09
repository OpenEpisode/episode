from __future__ import annotations

from fastapi import APIRouter, HTTPException

from episode.api.context import ApiContext
from episode.api.errors import PUBLIC_ERROR_RESPONSES
from episode.api.schemas import (
    EpisodeStartedWebhookSettingsResponse,
    EpisodeStartedWebhookSettingsUpdate,
    EpisodeStartedWebhookTestResponse,
)
from episode.notifications import EpisodeStartedWebhookSettingsService


def notifications_router(context: ApiContext) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/settings/notifications/episode-started",
        tags=["settings"],
        responses=PUBLIC_ERROR_RESPONSES,
    )

    def service() -> EpisodeStartedWebhookSettingsService:
        if not context.episode_started_webhook:
            raise HTTPException(503, "Episode-started webhook service is unavailable")
        return context.episode_started_webhook

    @router.get("", response_model=EpisodeStartedWebhookSettingsResponse)
    async def get_episode_started_webhook_settings():
        settings = await service().get_settings()
        return settings.public()

    @router.put("", response_model=EpisodeStartedWebhookSettingsResponse)
    async def update_episode_started_webhook_settings(
        payload: EpisodeStartedWebhookSettingsUpdate,
    ):
        try:
            settings = await service().update(
                enabled=payload.enabled,
                payload_format=payload.payload_format,
                timeout_seconds=payload.timeout_seconds,
                url=payload.url,
                clear_url=payload.clear_url,
            )
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return settings.public()

    @router.post("/test", response_model=EpisodeStartedWebhookTestResponse)
    async def test_episode_started_webhook():
        result = await service().test()
        return {
            "success": result.success,
            "message": result.message,
            "status_code": result.status_code,
        }

    return router
