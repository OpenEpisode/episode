from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict
from typing import Any

from episode.domain.models import Area, Device

_INVENTORY_BOOTSTRAP_KEY = "inventory.bootstrap.version"
_INVENTORY_BOOTSTRAP_VERSION = "1"
_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_LEGACY_VENDOR_DEVICE_TYPES = {"hikvision", "dahua", "reolink", "tplink", "tp-link"}


class InventoryConflictError(ValueError):
    """A requested inventory change would break a persisted relationship."""


class InventoryService:
    """Own persistent Area and Device inventory independently of file config."""

    def __init__(self, repository) -> None:
        self._repo = repository
        self._restart_required = False

    @property
    def restart_required(self) -> bool:
        return self._restart_required

    def mark_runtime_current(self) -> None:
        self._restart_required = False

    async def bootstrap(
        self,
        configured_areas: list[dict[str, Any]],
        configured_devices: list[dict[str, Any]],
    ) -> bool:
        """Import legacy file inventory once; later restarts leave SQLite authoritative."""
        if await self._repo.get_setting(_INVENTORY_BOOTSTRAP_KEY):
            return False

        for value in configured_areas:
            area = Area(**value)
            self._validate_identity(area.id, "Area")
            await self._repo.upsert_area(area)

        for value in configured_devices:
            device = Device(**value)
            self._normalize_device_type(device)
            self._validate_identity(device.id, "Device")
            if not await self._repo.get_area(device.area_id):
                raise InventoryConflictError(
                    f"Device {device.id!r} references unknown Area {device.area_id!r}."
                )
            await self._repo.upsert_device(device)

        await self._repo.set_setting(
            _INVENTORY_BOOTSTRAP_KEY,
            _INVENTORY_BOOTSTRAP_VERSION,
        )
        return bool(configured_areas or configured_devices)

    async def available_area_id(self, name: str) -> str:
        return await self._available_id(name, self._repo.get_area, "area")

    async def available_device_id(self, name: str) -> str:
        return await self._available_id(name, self._repo.get_device, "device")

    async def save_area(self, area: Area, *, create: bool) -> Area:
        self._validate_identity(area.id, "Area")
        existing = await self._repo.get_area(area.id)
        if create and existing:
            raise InventoryConflictError(f"Area {area.id!r} already exists.")
        if not create and not existing:
            raise KeyError(area.id)
        if not area.enabled:
            devices = await self._repo.list_devices(area.id, include_disabled=True)
            if any(device.enabled for device in devices):
                raise InventoryConflictError(
                    "Disable or move active Devices before disabling this Area."
                )
        return await self._repo.upsert_area(area)

    async def delete_area(self, area_id: str) -> None:
        if not await self._repo.get_area(area_id):
            raise KeyError(area_id)
        usage = await self._repo.area_usage(area_id)
        if any(usage.values()):
            raise InventoryConflictError(
                "This Area has Devices or incident history. Disable it instead of deleting it."
            )
        await self._repo.delete_area(area_id)

    async def save_device(self, device: Device, *, create: bool) -> Device:
        self._normalize_device_type(device)
        self._validate_identity(device.id, "Device")
        existing = await self._repo.get_device(device.id)
        if create and existing:
            raise InventoryConflictError(f"Device {device.id!r} already exists.")
        if not create and not existing:
            raise KeyError(device.id)

        area = await self._repo.get_area(device.area_id)
        if not area:
            raise InventoryConflictError(f"Area {device.area_id!r} does not exist.")
        if device.enabled and not area.enabled:
            raise InventoryConflictError("An active Device must belong to an active Area.")

        if device.ip_address:
            same_ip = await self._repo.find_device_by_ip(device.ip_address)
            if same_ip and same_ip.id != device.id:
                raise InventoryConflictError(
                    f"Network address {device.ip_address!r} is already used by {same_ip.name}."
                )

        saved = await self._repo.upsert_device(device)
        self._restart_required = True
        return saved

    async def delete_device(self, device_id: str) -> None:
        if not await self._repo.get_device(device_id):
            raise KeyError(device_id)
        usage = await self._repo.device_usage(device_id)
        if any(usage.values()):
            raise InventoryConflictError(
                "This Device has incident history. Disable it instead of deleting it."
            )
        await self._repo.delete_device(device_id)
        self._restart_required = True

    async def area_usage(self, area_id: str) -> dict[str, int]:
        return await self._repo.area_usage(area_id)

    async def device_usage(self, device_id: str) -> dict[str, int]:
        return await self._repo.device_usage(device_id)

    async def configured_devices(self) -> tuple[dict[str, Any], ...]:
        devices = await self._repo.list_devices()
        return tuple(asdict(device) for device in devices)

    @staticmethod
    def _normalize_device_type(device: Device) -> None:
        if device.device_type.lower() not in _LEGACY_VENDOR_DEVICE_TYPES:
            return
        device.device_type = "doorbell" if "doorbell" in device.capabilities else "camera"

    @staticmethod
    async def _available_id(name: str, getter, fallback: str) -> str:
        normalized = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
        base = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-") or fallback
        base = base[:64].rstrip("-")
        candidate = base
        suffix = 2
        while await getter(candidate):
            ending = f"-{suffix}"
            candidate = f"{base[: 64 - len(ending)].rstrip('-')}{ending}"
            suffix += 1
        return candidate

    @staticmethod
    def _validate_identity(value: str, label: str) -> None:
        if not _ID_PATTERN.fullmatch(value):
            raise InventoryConflictError(
                f"{label} ID must use lowercase letters, numbers, hyphens, or underscores."
            )
