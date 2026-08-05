from __future__ import annotations

from datetime import datetime

from fastapi import FastAPI
from pydantic import BaseModel, Field

from episode.plugins.manager import PluginManager
from episode.plugins.models import PluginInstanceState, PluginState


class PluginDeviceInfoResponse(BaseModel):
    manufacturer: str | None = None
    model: str | None = None
    firmware_version: str | None = None


class PluginInstanceResponse(BaseModel):
    id: str
    name: str
    state: PluginInstanceState
    messages_received: int = 0
    connected_at: datetime | None = None
    last_message_at: datetime | None = None
    error: str | None = None
    device_info: PluginDeviceInfoResponse | None = None


class PluginResponse(BaseModel):
    id: str
    name: str
    kind: str
    state: PluginState
    version: str | None = None
    architecture: str | None = None
    error: str | None = None
    instances: list[PluginInstanceResponse] = Field(default_factory=list)


def register_plugins_api(app: FastAPI, manager: PluginManager) -> None:
    @app.get("/api/v1/plugins", response_model=list[PluginResponse])
    async def list_plugins():
        return manager.statuses()
