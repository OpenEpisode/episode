from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable

from episode.plugins.models import (
    ManagedPlugin,
    PluginContext,
    PluginRegistration,
    PluginState,
    PluginStatus,
)

logger = logging.getLogger(__name__)


class PluginManager:
    def __init__(
        self,
        registrations: Iterable[PluginRegistration],
        context: PluginContext,
    ):
        self._registrations: list[PluginRegistration] = []
        self._context = context
        self._plugins: list[tuple[PluginRegistration, ManagedPlugin]] = []
        self._started = False
        self._statuses: dict[str, PluginStatus] = {}
        self.configure(registrations, context)

    def configure(
        self,
        registrations: Iterable[PluginRegistration],
        context: PluginContext,
    ) -> None:
        if self._started or self._plugins:
            raise RuntimeError("Cannot reconfigure plugins while they are running")
        self._registrations = list(registrations)
        self._context = context
        self._statuses = {
            registration.id: registration.validating_status()
            for registration in self._registrations
        }

    def statuses(self) -> list[dict]:
        for registration, plugin in self._plugins:
            status = plugin.status()
            self._validate_status(registration, status)
            self._statuses[registration.id] = status
        return [self._statuses[registration.id].public() for registration in self._registrations]

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        for registration in self._registrations:
            logger.info("Starting configured plugin %s", registration.id)
            try:
                plugin = registration.factory(self._context)
                self._plugins.append((registration, plugin))
                await plugin.start()
                status = plugin.status()
                self._validate_status(registration, status)
                self._statuses[registration.id] = status
            except asyncio.CancelledError:
                await self.stop()
                raise
            except Exception:
                logger.exception("Plugin %s failed during startup", registration.id)
                self._statuses[registration.id] = PluginStatus(
                    id=registration.id,
                    name=registration.name,
                    kind=registration.kind,
                    state=PluginState.FAILED,
                    error="Plugin startup failed. See the Episode log for details.",
                )
                continue

            if status.state == PluginState.READY:
                logger.info(
                    "Plugin %s is ready%s",
                    registration.id,
                    f" (version {status.version})" if status.version else "",
                )
            elif status.state == PluginState.NOT_INSTALLED:
                logger.warning("Configured plugin %s is not installed", registration.id)
            elif status.state != PluginState.VALIDATING:
                logger.warning(
                    "Plugin %s is %s: %s",
                    registration.id,
                    status.state,
                    status.error,
                )

    async def stop(self) -> None:
        plugins = list(reversed(self._plugins))
        self._plugins.clear()
        self._started = False
        for registration, plugin in plugins:
            try:
                await plugin.stop()
            except Exception:
                logger.exception("Plugin %s failed during shutdown", registration.id)

    @staticmethod
    def _validate_status(
        registration: PluginRegistration,
        status: PluginStatus,
    ) -> None:
        if status.id != registration.id:
            raise ValueError(
                f"Plugin returned status for {status.id!r}; expected {registration.id!r}"
            )
        if status.name != registration.name or status.kind != registration.kind:
            raise ValueError(f"Plugin {registration.id!r} returned inconsistent metadata")
