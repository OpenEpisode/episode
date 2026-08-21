from __future__ import annotations

import json

import aiosqlite

from episode.domain.models import Area, Device


class InventoryStore:
    """Persist Areas and Devices without exposing inventory SQL to the repository."""

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

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
        await self._connection.execute(
            """INSERT INTO devices (
                id, name, device_type, area_id,
                capabilities, ip_address, username, password,
                configs, activity_window_seconds, metadata, enabled
            )
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                   enabled=excluded.enabled""",
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
                json.dumps(device.metadata),
                int(device.enabled),
            ),
        )
        await self._connection.commit()
        return device

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
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = await self._connection.execute_fetchall(
            f"SELECT * FROM devices{where} ORDER BY name", params
        )
        return [self._row_to_device(row) for row in rows]

    async def delete_device(self, device_id: str) -> None:
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
                    (SELECT COUNT(*) FROM ingestion_receipts WHERE device_id = ?) AS receipts""",
                (device_id, device_id, device_id),
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
            metadata=json.loads(row["metadata"]),
            enabled=bool(row["enabled"]),
        )
