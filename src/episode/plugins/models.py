from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Callable, Protocol


class PluginState(StrEnum):
    NOT_INSTALLED = "not_installed"
    INCOMPLETE = "incomplete"
    INCOMPATIBLE = "incompatible"
    VALIDATING = "validating"
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True)
class PluginStatus:
    id: str
    name: str
    kind: str
    state: PluginState
    version: str | None = None
    architecture: str | None = None
    error: str | None = None

    def public(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class PluginContext:
    plugins_dir: Path
    configured_devices: tuple[Mapping[str, object], ...] = ()


class ManagedPlugin(Protocol):
    def status(self) -> PluginStatus: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


PluginFactory = Callable[[PluginContext], ManagedPlugin]


@dataclass(frozen=True)
class PluginRegistration:
    id: str
    name: str
    kind: str
    activation_capability: str
    factory: PluginFactory

    def validating_status(self) -> PluginStatus:
        return PluginStatus(
            id=self.id,
            name=self.name,
            kind=self.kind,
            state=PluginState.VALIDATING,
        )
