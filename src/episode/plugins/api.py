from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel

from episode.plugins.manager import PluginManager
from episode.plugins.models import PluginState


class PluginResponse(BaseModel):
    id: str
    name: str
    kind: str
    state: PluginState
    version: str | None = None
    architecture: str | None = None
    error: str | None = None


def register_plugins_api(app: FastAPI, manager: PluginManager) -> None:
    @app.get("/api/v1/plugins", response_model=list[PluginResponse])
    async def list_plugins():
        return manager.statuses()
