from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Iterable, Mapping
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
DEFAULT_PLUGIN_STARTUP_TIMEOUT = 60.0
DEFAULT_PLUGIN_SHUTDOWN_TIMEOUT = 15.0
_SENSITIVE_STATUS_KEYS = {
    "api_key",
    "auth_token",
    "authorization",
    "bearer_token",
    "cookie",
    "password",
    "session_token",
    "secret",
    "access_token",
    "refresh_token",
}


class _PluginLifecycleTimeoutError(Exception):
    pass


class _PluginOperationCancelledError(Exception):
    pass


def _consume_task_result(task: asyncio.Task) -> None:
    try:
        task.exception()
    except asyncio.CancelledError:
        pass


async def _cancel_task(task: asyncio.Task, timeout: float) -> None:
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=min(timeout, 1.0))
    if task in done:
        _consume_task_result(task)
    else:
        task.add_done_callback(_consume_task_result)


async def _bounded(operation: Awaitable[None], timeout: float) -> None:
    """Bound lifecycle waits without confusing a plugin's own TimeoutError."""
    task = asyncio.ensure_future(operation)
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
    except asyncio.CancelledError:
        await _cancel_task(task, timeout)
        raise
    if task in done:
        try:
            await task
        except asyncio.CancelledError as error:
            raise _PluginOperationCancelledError from error
        return
    await _cancel_task(task, timeout)
    raise _PluginLifecycleTimeoutError


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
        return {
            key: (
                "[redacted]"
                if key.lower().replace("-", "_") in _SENSITIVE_STATUS_KEYS
                else _json_safe(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    raise TypeError(f"Plugin status contains unsupported {value.__class__.__name__}")


class PluginManager:
    def __init__(
        self,
        registrations: Iterable[PluginRegistration],
        context: PluginContext,
        *,
        startup_timeout: float = DEFAULT_PLUGIN_STARTUP_TIMEOUT,
        shutdown_timeout: float = DEFAULT_PLUGIN_SHUTDOWN_TIMEOUT,
    ):
        if startup_timeout <= 0 or shutdown_timeout <= 0:
            raise ValueError("Plugin lifecycle timeouts must be greater than zero")
        self._registrations: list[PluginRegistration] = []
        self._context = context
        self._startup_timeout = startup_timeout
        self._shutdown_timeout = shutdown_timeout
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
                await _bounded(plugin.start(), self._startup_timeout)
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
            except _PluginLifecycleTimeoutError:
                await self._stop_partial(registration, plugin)
                logger.warning(
                    "Plugin %s did not start within %ss",
                    registration.id,
                    f"{self._startup_timeout:g}",
                )
                self._statuses[registration.id] = PluginStatus(
                    id=registration.id,
                    name=registration.name,
                    kind=registration.kind,
                    state=PluginState.FAILED,
                    version=registration.installed_version,
                    error=f"Plugin startup timed out after {self._startup_timeout:g}s.",
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

    async def _stop_partial(
        self,
        registration: PluginRegistration,
        plugin: ManagedPlugin | None,
    ) -> None:
        if plugin is None:
            return
        try:
            await _bounded(plugin.stop(), self._shutdown_timeout)
        except _PluginLifecycleTimeoutError:
            logger.warning(
                "Plugin %s cleanup timed out after %ss",
                registration.id,
                f"{self._shutdown_timeout:g}",
            )
        except asyncio.CancelledError:
            logger.warning("Plugin %s cleanup was cancelled", registration.id)
        except Exception:
            logger.exception("Plugin %s failed while cleaning up startup", registration.id)

    async def stop(self) -> None:
        plugins = list(reversed(self._plugins))
        self._plugins.clear()
        self._started = False
        cancellation: asyncio.CancelledError | None = None
        for registration, plugin in plugins:
            try:
                await _bounded(plugin.stop(), self._shutdown_timeout)
            except _PluginLifecycleTimeoutError:
                logger.warning(
                    "Plugin %s shutdown timed out after %ss",
                    registration.id,
                    f"{self._shutdown_timeout:g}",
                )
            except asyncio.CancelledError as error:
                cancellation = cancellation or error
                logger.warning("Plugin %s was cancelled during shutdown", registration.id)
            except Exception:
                logger.exception("Plugin %s failed during shutdown", registration.id)
        if cancellation:
            raise cancellation

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
                "activation_config_type": registration.activation_config_type,
                "configured_device_ids": list(registration.configured_device_ids),
                "capabilities": list(registration.integration.capabilities),
            }
        public = _json_safe(result)
        if not isinstance(public, dict):
            raise TypeError("Plugin status must be a mapping")
        return public
