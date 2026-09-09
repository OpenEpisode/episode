from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Response

from episode import __version__
from episode.api.context import ApiContext
from episode.api.errors import PUBLIC_ERROR_RESPONSES
from episode.api.schemas import (
    DiagnosticsExportResponse,
    DiagnosticsResponse,
    EpisodeLifecycleSettingsResponse,
    EpisodeLifecycleSettingsUpdate,
    HealthResponse,
    RetentionSettingsResponse,
    RetentionSettingsUpdate,
    SystemStatusResponse,
)

_SENSITIVE_KEY = re.compile(
    r"(^|[_-])(password|passwd|secret|api[_-]?key|authorization|cookie|credentials?|private[_-]?key)([_-]|$)",
    re.IGNORECASE,
)

_RETENTION_NOTICE = (
    "Episode manages recordings, snapshots, embedded images, and visual derivatives under "
    "this policy. Requirements vary by jurisdiction and use case. Exported or externally "
    "stored copies are not managed by Episode."
)


def _storage_summary(data_dir: str) -> dict[str, int | None]:
    data_bytes = 0
    stack = [data_dir] if data_dir and os.path.isdir(data_dir) else []
    while stack:
        directory = stack.pop()
        try:
            entries = os.scandir(directory)
        except OSError:
            continue
        with entries:
            for entry in entries:
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        data_bytes += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue

    total_bytes = None
    free_bytes = None
    if data_dir and os.path.isdir(data_dir):
        try:
            filesystem = os.statvfs(data_dir)
            total_bytes = filesystem.f_frsize * filesystem.f_blocks
            free_bytes = filesystem.f_frsize * filesystem.f_bavail
        except OSError:
            pass
    return {
        "data_bytes": data_bytes,
        "filesystem_total_bytes": total_bytes,
        "filesystem_free_bytes": free_bytes,
    }


def _sanitize(value, private_path: str):
    if isinstance(value, dict):
        return {
            str(key): (
                "[redacted]" if _SENSITIVE_KEY.search(str(key)) else _sanitize(item, private_path)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize(item, private_path) for item in value]
    if isinstance(value, tuple):
        return [_sanitize(item, private_path) for item in value]
    if isinstance(value, str) and private_path:
        return value.replace(os.path.abspath(private_path), "<data-dir>")
    return value


def system_router(context: ApiContext) -> APIRouter:
    router = APIRouter(responses=PUBLIC_ERROR_RESPONSES)

    def current_status():
        if context.operations:
            return context.operations.status()
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

    async def current_diagnostics():
        diagnostics = (
            context.operations.diagnostics()
            if context.operations
            else {"status": current_status(), "services": [], "integrations": []}
        )
        diagnostics["storage"] = await asyncio.to_thread(_storage_summary, context.data_dir)
        if context.retention:
            await context.retention.get_policy()
            retention = context.retention.status()
            diagnostics["retention"] = retention
        diagnostics["recording_issues"] = []
        if hasattr(context.repository, "list_evidence"):
            incomplete = await context.repository.list_evidence(
                evidence_type="incomplete_recording",
                available_only=True,
                limit=10,
            )
            diagnostics["recording_issues"] = [
                {
                    "evidence_id": item.id,
                    "episode_id": item.episode_id,
                    "device_id": item.device_id,
                    "timestamp": item.timestamp,
                    "reason": item.metadata.get("reason"),
                }
                for item in incomplete
            ]
        return _sanitize(diagnostics, context.data_dir)

    @router.get("/health", response_model=HealthResponse)
    async def health():
        return {"status": "ok", "version": __version__}

    @router.get("/api/v1/status", response_model=SystemStatusResponse)
    async def system_status():
        return current_status()

    @router.get(
        "/api/v1/settings/retention",
        response_model=RetentionSettingsResponse,
    )
    async def retention_settings():
        if not context.retention:
            raise HTTPException(503, "Retention service is unavailable")
        await context.retention.get_policy()
        status = context.retention.status()
        return {**status, "notice": _RETENTION_NOTICE}

    @router.put(
        "/api/v1/settings/retention",
        response_model=RetentionSettingsResponse,
    )
    async def update_retention_settings(request: RetentionSettingsUpdate):
        if not context.retention:
            raise HTTPException(503, "Retention service is unavailable")
        await context.retention.set_policy(
            enabled=request.enabled,
            retention_days=request.retention_days,
        )
        status = context.retention.status()
        return {**status, "notice": _RETENTION_NOTICE}

    @router.get(
        "/api/v1/settings/episode",
        response_model=EpisodeLifecycleSettingsResponse,
    )
    async def episode_lifecycle_settings():
        if not context.engine:
            raise HTTPException(503, "Episode lifecycle service is unavailable")
        return context.engine.lifecycle_settings()

    @router.put(
        "/api/v1/settings/episode",
        response_model=EpisodeLifecycleSettingsResponse,
    )
    async def update_episode_lifecycle_settings(
        request: EpisodeLifecycleSettingsUpdate,
    ):
        if not context.engine:
            raise HTTPException(503, "Episode lifecycle service is unavailable")
        try:
            return await context.engine.set_quiescent_grace(request.quiescent_grace_seconds)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @router.get("/api/v1/diagnostics", response_model=DiagnosticsResponse)
    async def diagnostics():
        return await current_diagnostics()

    @router.get("/api/v1/diagnostics/export", response_model=DiagnosticsExportResponse)
    async def diagnostics_export(response: Response):
        response.headers["Content-Disposition"] = 'attachment; filename="episode-diagnostics.json"'
        return {
            "schema_version": 1,
            "generated_at": datetime.now(tz=timezone.utc),
            "diagnostics": await current_diagnostics(),
        }

    return router
