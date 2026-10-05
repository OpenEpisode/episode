from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Response

from episode import __version__
from episode.api.context import ApiContext
from episode.api.errors import PUBLIC_ERROR_RESPONSES
from episode.api.schemas import (
    AlertResponse,
    DiagnosticsExportResponse,
    DiagnosticsResponse,
    EpisodeLifecycleSettingsResponse,
    EpisodeLifecycleSettingsUpdate,
    HealthResponse,
    InstallationSettingsResponse,
    InstallationSettingsUpdate,
    RetentionSettingsResponse,
    RetentionSettingsUpdate,
    SystemStatusResponse,
)
from episode.installation import get_external_episode_url, set_external_episode_url

_SENSITIVE_KEY = re.compile(
    r"(^|[_-])(password|passwd|secret|api[_-]?key|authorization|cookie|credentials?|private[_-]?key)([_-]|$)",
    re.IGNORECASE,
)

_RETENTION_NOTICE = (
    "Episode manages recordings, snapshots, embedded images, and visual derivatives under "
    "this policy. Requirements vary by jurisdiction and use case. Exported or externally "
    "stored copies are not managed by Episode."
)

_ALERT_VALIDATION_KEYS = (
    "valid",
    "error",
    "fragment_count",
    "referenced_fragment_count",
    "unreferenced_fragment_count",
    "empty_fragment_count",
    "preserved_temporary_component_count",
    "temporary_components_preserved",
    "playlist_temporary_preserved",
)


def _safe_alert_playlist_validation(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, object] = {}
    for key in _ALERT_VALIDATION_KEYS:
        item = value.get(key)
        if key == "error":
            if isinstance(item, str):
                result[key] = item[:120]
        elif key == "valid" or key.endswith("_preserved"):
            if isinstance(item, bool):
                result[key] = item
        elif isinstance(item, int) and not isinstance(item, bool) and item >= 0:
            result[key] = item
    return result


def _safe_exit_code(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


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

    @router.get("/api/v1/alerts", response_model=list[AlertResponse])
    async def alerts(
        limit: int = Query(
            default=50,
            ge=1,
            le=50,
            description="Maximum number of active recording alerts to return.",
        ),
        offset: int = Query(
            default=0,
            ge=0,
            description="Number of alerts to skip in newest-first order.",
        ),
    ):
        evidence_items = await context.repository.list_finalization_alerts(
            limit=limit,
            offset=offset,
        )
        alerts = []
        for evidence in evidence_items:
            reason = evidence.metadata.get("reason")
            validation = evidence.metadata.get("playlist_validation")
            validation = _safe_alert_playlist_validation(validation)
            fragment_count = evidence.metadata.get("fragment_count")
            if (
                isinstance(fragment_count, int)
                and not isinstance(fragment_count, bool)
                and fragment_count >= 0
            ):
                validation["fragment_count"] = fragment_count
            if reason == "invalid_hls_playlist":
                code = "invalid_hls_playlist"
                title = "Recording playlist failed validation"
                error = validation.get("error")
                message = (
                    "The recording was preserved as incomplete because its HLS playlist "
                    f"could not be validated{f' ({error})' if error else ''}."
                )
            else:
                code = "incomplete_hls_finalization"
                title = "Recording finalization was incomplete"
                message = (
                    "The recording was preserved as incomplete because finalization left "
                    "temporary or otherwise incomplete recording components."
                )
            alerts.append(
                {
                    "id": evidence.id,
                    "severity": "warning",
                    "code": code,
                    "title": title,
                    "message": message,
                    "created_at": evidence.timestamp,
                    "device_id": evidence.device_id,
                    "episode_id": evidence.episode_id,
                    "evidence_id": evidence.id,
                    "playlist_validation": {
                        key: validation[key] for key in _ALERT_VALIDATION_KEYS if key in validation
                    },
                    "ffmpeg_exit_code": _safe_exit_code(evidence.metadata.get("ffmpeg_exit_code")),
                }
            )
        return alerts

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

    @router.get(
        "/api/v1/settings/installation",
        response_model=InstallationSettingsResponse,
    )
    async def installation_settings():
        return {"external_url": await get_external_episode_url(context.repository)}

    @router.put(
        "/api/v1/settings/installation",
        response_model=InstallationSettingsResponse,
    )
    async def update_installation_settings(request: InstallationSettingsUpdate):
        try:
            external_url = await set_external_episode_url(
                context.repository,
                request.external_url,
            )
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return {"external_url": external_url}

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
