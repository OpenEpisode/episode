from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Iterable, Mapping
from copy import deepcopy
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
        self._inventory_snapshots: dict[str, object] = {}
        self._pending_reconcile_ids: set[str] = set()
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
        self._pending_reconcile_ids.clear()
        self._statuses = {
            registration.id: registration.validating_status()
            for registration in self._registrations
        }
        self._inventory_snapshots = {
            registration.id: self._inventory_snapshot(registration, context)
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
            await self._start_registration(registration, stop_all_on_cancel=True)
        self._pending_reconcile_ids.clear()

    async def reconcile(
        self,
        registrations: Iterable[PluginRegistration],
        context: PluginContext,
        *,
        changed_device_id: str | None = None,
    ) -> None:
        """Reload only registrations whose inputs or metadata changed.

        ``changed_device_id`` narrows inventory comparison to the row known to
        have changed. This avoids treating unrelated database drift in a
        freshly loaded context as a plugin configuration change.
        """
        if not self._started:
            self.configure(registrations, context)
            await self.start()
            return

        new_registrations = list(registrations)
        new_by_id = {registration.id: registration for registration in new_registrations}
        new_snapshots = {
            registration.id: self._inventory_snapshot(registration, context)
            for registration in new_registrations
        }
        old_by_id = {registration.id: registration for registration in self._registrations}
        changed_ids = {
            plugin_id
            for plugin_id in old_by_id.keys() | new_by_id.keys()
            if (
                plugin_id not in old_by_id
                or plugin_id not in new_by_id
                or old_by_id.get(plugin_id) != new_by_id.get(plugin_id)
                or self._inventory_changed(
                    self._inventory_snapshots.get(plugin_id),
                    new_snapshots[plugin_id],
                    changed_device_id,
                )
            )
        }
        self._pending_reconcile_ids.intersection_update(new_by_id)
        self._pending_reconcile_ids.update(
            plugin_id for plugin_id in changed_ids if plugin_id in new_by_id
        )
        changed_ids.update(self._pending_reconcile_ids)

        active = {registration.id: plugin for registration, plugin in self._plugins}
        self._plugins = [
            (registration, active[registration.id])
            for registration in new_registrations
            if registration.id in active and registration.id not in changed_ids
        ]
        cancellation: asyncio.CancelledError | None = None
        to_stop = [
            (registration, active[registration.id])
            for registration in self._registrations
            if registration.id in changed_ids and registration.id in active
        ]
        for registration, plugin in reversed(to_stop):
            try:
                await self._stop_plugin(registration, plugin)
            except asyncio.CancelledError as error:
                cancellation = cancellation or error

        self._registrations = new_registrations
        self._context = context
        self._inventory_snapshots = {
            registration.id: (
                new_snapshots[registration.id]
                if registration.id in changed_ids or changed_device_id is None
                else self._merge_inventory_device(
                    self._inventory_snapshots.get(registration.id),
                    new_snapshots[registration.id],
                    changed_device_id,
                )
            )
            for registration in new_registrations
        }
        self._statuses = {
            registration.id: self._statuses[registration.id]
            for registration in new_registrations
            if registration.id not in changed_ids and registration.id in self._statuses
        }
        for registration in new_registrations:
            if registration.id in changed_ids:
                self._statuses[registration.id] = registration.validating_status()

        if cancellation:
            for registration in new_registrations:
                if registration.id in changed_ids and registration.unavailable_state is None:
                    self._statuses[registration.id] = self._interrupted_status(registration)
            raise cancellation

        for registration in new_registrations:
            if registration.id in changed_ids:
                try:
                    await self._start_registration(registration, stop_all_on_cancel=False)
                except asyncio.CancelledError:
                    self._statuses[registration.id] = self._interrupted_status(registration)
                    raise
                else:
                    self._pending_reconcile_ids.discard(registration.id)

    @staticmethod
    def _interrupted_status(registration: PluginRegistration) -> PluginStatus:
        return PluginStatus(
            id=registration.id,
            name=registration.name,
            kind=registration.kind,
            state=PluginState.FAILED,
            version=registration.installed_version,
            error=(
                "Plugin reconfiguration was interrupted; save a Device or restart Episode to retry."
            ),
        )

    async def _start_registration(
        self,
        registration: PluginRegistration,
        *,
        stop_all_on_cancel: bool,
    ) -> None:
        """Start one registration using the same failure isolation as startup."""
        if registration.unavailable_state is not None:
            logger.warning(
                "Configured plugin %s is %s: %s",
                registration.id,
                registration.unavailable_state,
                registration.unavailable_error,
            )
            return
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
            if stop_all_on_cancel:
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
        except Exception:
            await self._stop_partial(registration, plugin)
            logger.exception("Plugin %s failed during startup", registration.id)
            self._statuses[registration.id] = PluginStatus(
                id=registration.id,
                name=registration.name,
                kind=registration.kind,
                state=PluginState.FAILED,
                version=registration.installed_version,
                error="Plugin startup failed. See the Episode log for details.",
            )
        else:
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

    async def _stop_plugin(
        self,
        registration: PluginRegistration,
        plugin: ManagedPlugin,
    ) -> None:
        try:
            await _bounded(plugin.stop(), self._shutdown_timeout)
        except _PluginLifecycleTimeoutError:
            logger.warning(
                "Plugin %s shutdown timed out after %ss",
                registration.id,
                f"{self._shutdown_timeout:g}",
            )
        except asyncio.CancelledError:
            logger.warning("Plugin %s was cancelled during shutdown", registration.id)
            raise
        except Exception:
            logger.exception("Plugin %s failed during shutdown", registration.id)

    @staticmethod
    def _inventory_changed(
        previous: object,
        current: object,
        changed_device_id: str | None,
    ) -> bool:
        if changed_device_id is None:
            return previous != current
        return PluginManager._inventory_device(previous, changed_device_id) != (
            PluginManager._inventory_device(current, changed_device_id)
        )

    @staticmethod
    def _inventory_device(snapshot: object, device_id: str) -> object:
        if not isinstance(snapshot, tuple):
            return None
        device = next(
            (
                device
                for device in snapshot
                if isinstance(device, Mapping) and str(device.get("id", "")) == device_id
            ),
            None,
        )
        return deepcopy(device)

    @staticmethod
    def _merge_inventory_device(previous: object, current: object, device_id: str) -> object:
        """Advance only the edited row; concurrent saves may already be in `current`."""
        if not isinstance(previous, tuple):
            return current
        remaining = [
            device
            for device in previous
            if not (isinstance(device, Mapping) and str(device.get("id", "")) == device_id)
        ]
        updated = PluginManager._inventory_device(current, device_id)
        if updated is not None:
            remaining.append(updated)
        return tuple(sorted(remaining, key=lambda device: str(device.get("id", ""))))

    @staticmethod
    def _inventory_value(device: Mapping[str, object], field_name: str) -> object:
        """Read a projected inventory field without exposing mutable state.

        Plugin ids may contain dots, so lookup prefers the longest matching
        mapping key at each level. That keeps ``configs.example.plugin`` usable
        both for a nested path and for an external plugin whose id is
        ``example.plugin``.
        """
        parts = tuple(part for part in field_name.split(".") if part)
        if not parts:
            return None

        def lookup(value: object, remaining: tuple[str, ...]) -> object:
            if not remaining:
                return value
            if not isinstance(value, Mapping):
                return None
            for length in range(len(remaining), 0, -1):
                key = ".".join(remaining[:length])
                if key in value:
                    return lookup(value[key], remaining[length:])
            return None

        return deepcopy(lookup(device, parts))

    @staticmethod
    def _inventory_snapshot(
        registration: PluginRegistration,
        context: PluginContext,
    ) -> object:
        if registration.inventory_scope == "none":
            return ()
        devices = list(context.configured_devices)
        if registration.inventory_scope == "selected":
            configured_ids = set(registration.configured_device_ids)
            selected: list[Mapping[str, object]] = []
            for device in devices:
                configs = device.get("configs")
                has_activation = isinstance(configs, Mapping) and (
                    registration.activation_config_type in configs
                )
                if (
                    device.get("enabled", True)
                    and device.get("setup_state", "ready") == "ready"
                    and (has_activation or str(device.get("id", "")) in configured_ids)
                ):
                    selected.append(device)
            devices = selected
        snapshot: list[dict[str, object]] = []
        for device in devices:
            copied = deepcopy(dict(device))
            if registration.inventory_fields:
                copied = {"id": deepcopy(copied.get("id"))}
                for field_name in registration.inventory_fields:
                    if field_name == "id":
                        continue
                    copied[field_name] = PluginManager._inventory_value(device, field_name)
            snapshot.append(copied)
        snapshot.sort(key=lambda device: str(device.get("id", "")))
        return tuple(snapshot)

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
                "manufacturer_scope": list(registration.integration.manufacturer_scope),
                "manufacturer_scope_kind": registration.integration.manufacturer_scope_kind,
                "device_types": list(registration.integration.device_types),
            }
        public = _json_safe(result)
        if not isinstance(public, dict):
            raise TypeError("Plugin status must be a mapping")
        return public
