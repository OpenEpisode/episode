from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Response

from episode import __version__
from episode.api.context import ApiContext
from episode.api.errors import PUBLIC_ERROR_RESPONSES
from episode.api.schemas import (
    DiagnosticsExportResponse,
    DiagnosticsResponse,
    HealthResponse,
    SystemStatusResponse,
)

_SENSITIVE_KEY = re.compile(
    r"(^|[_-])(password|passwd|secret|api[_-]?key|authorization|cookie|credentials?|private[_-]?key)([_-]|$)",
    re.IGNORECASE,
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
        return _sanitize(diagnostics, context.data_dir)

    @router.get("/health", response_model=HealthResponse)
    async def health():
        return {"status": "ok", "version": __version__}

    @router.get("/api/v1/status", response_model=SystemStatusResponse)
    async def system_status():
        return current_status()

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
