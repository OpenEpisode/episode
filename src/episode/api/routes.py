from __future__ import annotations

from fastapi import FastAPI, Request

from episode import __version__
from episode.api.context import ApiContext
from episode.api.endpoints.episodes import episodes_router
from episode.api.endpoints.events import events_router
from episode.api.endpoints.evidence import evidence_router
from episode.api.endpoints.inventory import inventory_router
from episode.api.endpoints.receipts import receipts_router
from episode.api.endpoints.system import system_router
from episode.api.errors import install_error_handlers
from episode.api.runtime import OperationalView
from episode.inventory import DeviceValidationService, InventoryService
from episode.media.previews import CurrentViewService
from episode.media.timelapse import TimelapseService


def create_api(
    repo,
    data_dir: str = "",
    snapshot_window: int = 1,
    timelapses: TimelapseService | None = None,
    operations: OperationalView | None = None,
    inventory: InventoryService | None = None,
    validator: DeviceValidationService | None = None,
    current_views: CurrentViewService | None = None,
) -> FastAPI:
    app = FastAPI(
        title="Episode",
        description="Local-first, event-driven incident capture API",
        version=__version__,
    )
    context = ApiContext(
        repository=repo,
        data_dir=data_dir,
        snapshot_window=snapshot_window,
        timelapses=timelapses,
        operations=operations,
        inventory=inventory,
        validator=validator,
        current_views=current_views,
    )
    install_error_handlers(app)

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        return response

    app.include_router(system_router(context))
    app.include_router(inventory_router(context))
    app.include_router(episodes_router(context))
    app.include_router(events_router(context))
    app.include_router(receipts_router(context))
    app.include_router(evidence_router(context))

    return app
