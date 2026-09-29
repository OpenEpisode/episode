from __future__ import annotations

import asyncio
import json

import aiosqlite

from episode.domain.event_filter import decode_device_filter, encode_device_filter
from episode.domain.models import Area, CapabilityConfig, Device, DeviceDiscoveryUpdate

_SETUP_STATE_KEY = "_setup_state"


class InventoryStore:
    """Persist Areas and Devices without exposing inventory SQL to the repository."""

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection
        self._device_write_lock = asyncio.Lock()

    async def upsert_area(self, area: Area) -> Area:
        await self._connection.execute(
            """INSERT INTO areas (id, name, location, metadata, enabled)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   name=excluded.name,
                   location=excluded.location,
                   metadata=excluded.metadata,
                   enabled=excluded.enabled""",
            (area.id, area.name, area.location, json.dumps(area.metadata), int(area.enabled)),
        )
        await self._connection.commit()
        return area

    async def get_area(self, area_id: str) -> Area | None:
        rows = await self._connection.execute_fetchall(
            "SELECT * FROM areas WHERE id = ?", (area_id,)
        )
        return self._row_to_area(rows[0]) if rows else None

    async def list_areas(self, *, include_disabled: bool = False) -> list[Area]:
        query = "SELECT * FROM areas"
        if not include_disabled:
            query += " WHERE enabled = 1"
        rows = await self._connection.execute_fetchall(query + " ORDER BY name")
        return [self._row_to_area(row) for row in rows]

    async def delete_area(self, area_id: str) -> None:
        await self._connection.execute("DELETE FROM areas WHERE id = ?", (area_id,))
        await self._connection.commit()

    async def upsert_device(self, device: Device) -> Device:
        async with self._device_write_lock:
            return await self._upsert_device(device)

    async def _upsert_device(self, device: Device) -> Device:
        await self._connection.execute(
            """INSERT INTO devices (
                id, name, device_type, area_id,
                capabilities, ip_address, username, password,
                configs, activity_window_seconds, metadata, enabled,
                event_filter
            )
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   name=excluded.name,
                   device_type=excluded.device_type,
                   area_id=excluded.area_id,
                   capabilities=excluded.capabilities,
                   ip_address=excluded.ip_address,
                   username=excluded.username,
                   password=excluded.password,
                   configs=excluded.configs,
                   activity_window_seconds=excluded.activity_window_seconds,
                   metadata=excluded.metadata,
                   enabled=excluded.enabled,
                   event_filter=excluded.event_filter""",
            (
                device.id,
                device.name,
                device.device_type,
                device.area_id,
                json.dumps(device.capabilities),
                device.ip_address,
                device.username,
                device.password,
                json.dumps(
                    {
                        key: {
                            "protocol": value.protocol,
                            "port": value.port,
                            "path": value.path,
                            "settings": value.settings,
                        }
                        for key, value in device.configs.items()
                    }
                ),
                device.activity_window_seconds,
                json.dumps({**device.metadata, _SETUP_STATE_KEY: device.setup_state}),
                int(device.enabled),
                encode_device_filter(device.event_filter),
            ),
        )
        await self._connection.commit()
        return device

    async def apply_device_discovery(self, update: DeviceDiscoveryUpdate) -> Device | None:
        """Merge integration-owned discovery data into the current Device row.

        The row is read and written under the same lock as operator upserts so
        a late discovery result cannot overwrite a newer edit.  Discovery is
        deliberately ignored once the Device is disabled, awaiting setup, or
        no longer has the integration configuration that produced the result.
        """
        if not update.device_id or not update.integration_type:
            return None
        async with self._device_write_lock:
            rows = await self._connection.execute_fetchall(
                "SELECT * FROM devices WHERE id = ?", (update.device_id,)
            )
            if not rows:
                return None
            latest = self._row_to_device(rows[0])
            if not latest.can_participate:
                return None
            if update.integration_type not in latest.configs:
                return None

            capabilities = list(dict.fromkeys([*latest.capabilities, *update.capabilities]))
            metadata = dict(latest.metadata)
            metadata[update.integration_type] = dict(update.metadata)
            configs = dict(latest.configs)
            existing_video = latest.get_config("video")
            discovered_video = update.video_if_unconfigured
            if discovered_video is not None and (
                existing_video is None or (not existing_video.protocol and not existing_video.path)
            ):
                settings = dict(discovered_video.settings)
                if existing_video is not None:
                    settings = {**settings, **existing_video.settings}
                configs["video"] = CapabilityConfig(
                    protocol=discovered_video.protocol,
                    port=discovered_video.port,
                    path=discovered_video.path,
                    settings=settings,
                )

            merged = Device(
                id=latest.id,
                name=latest.name,
                device_type=latest.device_type,
                area_id=latest.area_id,
                capabilities=capabilities,
                ip_address=latest.ip_address,
                username=latest.username,
                password=latest.password,
                configs=configs,
                activity_window_seconds=latest.activity_window_seconds,
                metadata=metadata,
                enabled=latest.enabled,
                event_filter=latest.event_filter,
                setup_state=latest.setup_state,
            )
            return await self._upsert_device(merged)

    async def get_device(self, device_id: str) -> Device | None:
        rows = await self._connection.execute_fetchall(
            "SELECT * FROM devices WHERE id = ?", (device_id,)
        )
        return self._row_to_device(rows[0]) if rows else None

    async def find_device_by_ip(self, ip_address: str) -> Device | None:
        rows = await self._connection.execute_fetchall(
            "SELECT * FROM devices WHERE ip_address = ?", (ip_address,)
        )
        return self._row_to_device(rows[0]) if rows else None

    async def list_devices(
        self,
        area_id: str | None = None,
        *,
        include_disabled: bool = False,
    ) -> list[Device]:
        clauses: list[str] = []
        params: list[str] = []
        if area_id:
            clauses.append("area_id = ?")
            params.append(area_id)
        if not include_disabled:
            clauses.append("enabled = 1")
            clauses.append("COALESCE(json_extract(metadata, '$._setup_state'), 'ready') = 'ready'")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = await self._connection.execute_fetchall(
            f"SELECT * FROM devices{where} ORDER BY name", params
        )
        return [self._row_to_device(row) for row in rows]

    async def delete_device(self, device_id: str) -> None:
        async with self._device_write_lock:
            await self._connection.execute("DELETE FROM devices WHERE id = ?", (device_id,))
            await self._connection.commit()

    async def area_usage(self, area_id: str) -> dict[str, int]:
        row = (
            await self._connection.execute_fetchall(
                """SELECT
                    (SELECT COUNT(*) FROM devices WHERE area_id = ?) AS devices,
                    (SELECT COUNT(*) FROM episodes WHERE primary_area_id = ?) AS episodes,
                    (SELECT COUNT(*) FROM events WHERE area_id = ?) AS events,
                    (SELECT COUNT(*) FROM evidence WHERE area_id = ?) AS evidence,
                    (SELECT COUNT(*) FROM ingestion_receipts WHERE area_id = ?) AS receipts""",
                (area_id, area_id, area_id, area_id, area_id),
            )
        )[0]
        return {key: int(row[key]) for key in row.keys()}

    async def device_usage(self, device_id: str) -> dict[str, int]:
        row = (
            await self._connection.execute_fetchall(
                """SELECT
                    (SELECT COUNT(*) FROM events WHERE device_id = ?) AS events,
                    (SELECT COUNT(*) FROM evidence WHERE device_id = ?) AS evidence,
                    (SELECT COUNT(*) FROM ingestion_receipts WHERE device_id = ?) AS receipts,
                    (SELECT COUNT(*)
                       FROM capture_profiles profile, json_each(profile.device_ids) member
                      WHERE profile.builtin = 0 AND member.value = ?) AS capture_profiles""",
                (device_id, device_id, device_id, device_id),
            )
        )[0]
        return {key: int(row[key]) for key in row.keys()}

    @staticmethod
    def _row_to_area(row: aiosqlite.Row) -> Area:
        return Area(
            id=row["id"],
            name=row["name"],
            location=row["location"],
            metadata=json.loads(row["metadata"]),
            enabled=bool(row["enabled"]),
        )

    @staticmethod
    def _row_to_device(row: aiosqlite.Row) -> Device:
        metadata = json.loads(row["metadata"])
        setup_state = metadata.pop(_SETUP_STATE_KEY, "ready")
        return Device(
            id=row["id"],
            name=row["name"],
            device_type=row["device_type"],
            area_id=row["area_id"],
            capabilities=json.loads(row["capabilities"]) if row["capabilities"] else [],
            ip_address=row["ip_address"],
            username=row["username"],
            password=row["password"],
            configs=json.loads(row["configs"]) if row["configs"] else {},
            activity_window_seconds=row["activity_window_seconds"],
            metadata=metadata,
            enabled=bool(row["enabled"]),
            event_filter=decode_device_filter(
                row["event_filter"] if "event_filter" in row.keys() else None
            ),
            setup_state=setup_state,
        )
