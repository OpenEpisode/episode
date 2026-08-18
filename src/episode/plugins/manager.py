from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping
from datetime import datetime
from enum import Enum

from episode.plugin_api import PluginConfigurationError
from episode.plugins.models import (
    ManagedPlugin,
    PluginContext,
    PluginRegistration,
    PluginState,
    PluginStatus,
)

logger = logging.getLogger(__name__)


def _json_safe(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("Plugin status mappings must use string keys")
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    raise TypeError(f"Plugin status contains unsupported {value.__class__.__name__}")


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
            try:
                status = plugin.status()
                self._validate_status(registration, status)
                self._statuses[registration.id] = status
            except Exception:
                logger.exception("Plugin %s failed while reporting status", registration.id)
                self._statuses[registration.id] = PluginStatus(
                    id=registration.id,
                    name=registration.name,
                    kind=registration.kind,
                    state=PluginState.FAILED,
                    error="Plugin status is unavailable. See the Episode log for details.",
                )
        results = []
        for registration in self._registrations:
            try:
                result = self._public_status(registration, self._statuses[registration.id])
            except Exception:
                logger.exception("Plugin %s returned an invalid public status", registration.id)
                failed = PluginStatus(
                    id=registration.id,
                    name=registration.name,
                    kind=registration.kind,
                    state=PluginState.FAILED,
                    error="Plugin status is unavailable. See the Episode log for details.",
                )
                self._statuses[registration.id] = failed
                result = self._public_status(registration, failed)
            results.append(result)
        return results

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        for registration in self._registrations:
            if registration.unavailable_state is not None:
                logger.warning(
                    "Configured plugin %s is %s: %s",
                    registration.id,
                    registration.unavailable_state,
                    registration.unavailable_error,
                )
                continue
            logger.info("Starting configured plugin %s", registration.id)
            plugin: ManagedPlugin | None = None
            try:
                plugin = registration.factory(self._context)
                await plugin.start()
                status = plugin.status()
                self._validate_status(registration, status)
                self._plugins.append((registration, plugin))
                self._statuses[registration.id] = status
            except asyncio.CancelledError:
                await self._stop_partial(registration, plugin)
                await self.stop()
                raise
            except PluginConfigurationError as error:
                await self._stop_partial(registration, plugin)
                logger.warning("Plugin %s configuration is invalid: %s", registration.id, error)
                self._statuses[registration.id] = PluginStatus(
                    id=registration.id,
                    name=registration.name,
                    kind=registration.kind,
                    state=PluginState.FAILED,
                    version=registration.installed_version,
                    error=str(error),
                )
                continue
            except Exception:
                await self._stop_partial(registration, plugin)
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

    @staticmethod
    async def _stop_partial(
        registration: PluginRegistration,
        plugin: ManagedPlugin | None,
    ) -> None:
        if plugin is None:
            return
        try:
            await plugin.stop()
        except Exception:
            logger.exception("Plugin %s failed while cleaning up startup", registration.id)

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

    @staticmethod
    def _public_status(
        registration: PluginRegistration,
        status: PluginStatus,
    ) -> dict:
        result = status.public()
        if registration.integration:
            result["integration"] = {
                "type": registration.integration.type,
                "name": registration.integration.name,
                "device_scoped": registration.integration.device_scoped,
                "activation_capability": registration.activation_capability,
                "configured_device_ids": list(registration.configured_device_ids),
                "capabilities": list(registration.integration.capabilities),
            }
        public = _json_safe(result)
        if not isinstance(public, dict):
            raise TypeError("Plugin status must be a mapping")
        return public
