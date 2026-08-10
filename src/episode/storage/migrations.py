from __future__ import annotations

import aiosqlite


async def _tables(connection: aiosqlite.Connection) -> set[str]:
    rows = await connection.execute_fetchall("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {row["name"] for row in rows}


async def _columns(connection: aiosqlite.Connection, table: str) -> set[str]:
    rows = await connection.execute_fetchall(f"PRAGMA table_info({table})")
    return {row["name"] for row in rows}


async def _copy_identity_column(
    connection: aiosqlite.Connection,
    table: str,
    old_name: str,
    new_name: str,
) -> None:
    columns = await _columns(connection, table)
    if old_name not in columns:
        return
    if new_name not in columns:
        await connection.execute(
            f"ALTER TABLE {table} ADD COLUMN {new_name} TEXT NOT NULL DEFAULT ''"
        )
    await connection.execute(
        f"UPDATE {table} SET {new_name} = {old_name} WHERE ({new_name} IS NULL OR {new_name} = '')"
    )


async def migrate_legacy_identity_schema(connection: aiosqlite.Connection) -> None:
    """Migrate pre-alpha Asset/Sensor storage without discarding local evidence."""
    tables = await _tables(connection)

    if "assets" in tables:
        await connection.execute(
            """CREATE TABLE IF NOT EXISTS areas (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                location TEXT NOT NULL DEFAULT '',
                metadata TEXT NOT NULL DEFAULT '{}'
            )"""
        )
        await connection.execute(
            """INSERT OR IGNORE INTO areas (id, name, location, metadata)
               SELECT id, name, location, metadata FROM assets"""
        )

    if "sensors" in tables:
        await connection.execute(
            """CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                device_type TEXT NOT NULL DEFAULT '',
                area_id TEXT NOT NULL DEFAULT '',
                capabilities TEXT NOT NULL DEFAULT '[]',
                ip_address TEXT NOT NULL DEFAULT '',
                username TEXT NOT NULL DEFAULT '',
                password TEXT NOT NULL DEFAULT '',
                configs TEXT NOT NULL DEFAULT '{}',
                metadata TEXT NOT NULL DEFAULT '{}'
            )"""
        )
        await connection.execute(
            """INSERT OR IGNORE INTO devices (
                id, name, device_type, area_id, capabilities, ip_address,
                username, password, configs, metadata
            )
            SELECT id, name, sensor_type, asset_id, capabilities, ip_address,
                   username, password, configs, metadata
            FROM sensors"""
        )

    for table, old_name, new_name in (
        ("episodes", "primary_asset_id", "primary_area_id"),
        ("events", "sensor_id", "device_id"),
        ("events", "asset_id", "area_id"),
        ("evidence", "sensor_id", "device_id"),
        ("evidence", "asset_id", "area_id"),
        ("ingestion_receipts", "sensor_id", "device_id"),
        ("ingestion_receipts", "asset_id", "area_id"),
    ):
        if table in tables:
            await _copy_identity_column(connection, table, old_name, new_name)

    await connection.commit()


async def migrate_episode_activity_schema(connection: aiosqlite.Connection) -> None:
    """Add server-side episode activity time without changing event chronology."""
    tables = await _tables(connection)
    if "episodes" not in tables:
        return
    columns = await _columns(connection, "episodes")
    if "last_activity_at" not in columns:
        await connection.execute("ALTER TABLE episodes ADD COLUMN last_activity_at TEXT")
    await connection.execute(
        """UPDATE episodes
           SET last_activity_at = COALESCE(last_event_time, start_time)
           WHERE last_activity_at IS NULL"""
    )
    await connection.commit()


async def migrate_inventory_schema(connection: aiosqlite.Connection) -> None:
    """Add persistent inventory state without rewriting existing identities."""
    tables = await _tables(connection)
    for table in ("areas", "devices"):
        if table not in tables:
            continue
        columns = await _columns(connection, table)
        if "enabled" not in columns:
            await connection.execute(
                f"ALTER TABLE {table} ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"
            )
    if "devices" in tables:
        await connection.execute(
            """UPDATE devices
               SET device_type = CASE
                   WHEN lower(capabilities) LIKE '%"doorbell"%' THEN 'doorbell'
                   ELSE 'camera'
               END
               WHERE lower(device_type) IN (
                   'hikvision', 'dahua', 'reolink', 'tplink', 'tp-link'
               )"""
        )
    await connection.execute(
        """CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )"""
    )
    await connection.commit()
