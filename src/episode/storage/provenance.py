from __future__ import annotations

import json
from datetime import datetime

import aiosqlite

from episode.domain.models import IngestionReceipt, RawArtifact, ReceiptStatus


class ProvenanceStore:
    """Persistence boundary for immutable artifacts and connector receipts."""

    def __init__(self, connection: aiosqlite.Connection):
        self._conn = connection

    async def create_artifact(self, artifact: RawArtifact, *, commit: bool = True) -> RawArtifact:
        existing = await self.find_artifact_by_path(artifact.file_path)
        if existing:
            return existing
        await self._conn.execute(
            """INSERT INTO raw_artifacts (
                id, artifact_type, file_path, mime_type, byte_size, sha256,
                created_at, original_filename, sealed, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                artifact.id,
                artifact.artifact_type,
                artifact.file_path,
                artifact.mime_type,
                artifact.byte_size,
                artifact.sha256,
                artifact.created_at.isoformat(),
                artifact.original_filename,
                int(artifact.sealed),
                json.dumps(artifact.metadata),
            ),
        )
        if commit:
            await self._conn.commit()
        return artifact

    async def get_artifact(self, artifact_id: str) -> RawArtifact | None:
        rows = await self._conn.execute_fetchall(
            "SELECT * FROM raw_artifacts WHERE id = ?", (artifact_id,)
        )
        return self._row_to_artifact(rows[0]) if rows else None

    async def find_artifact_by_path(self, file_path: str) -> RawArtifact | None:
        rows = await self._conn.execute_fetchall(
            "SELECT * FROM raw_artifacts WHERE file_path = ?", (file_path,)
        )
        return self._row_to_artifact(rows[0]) if rows else None

    async def update_artifact_path(self, artifact_id: str, file_path: str) -> None:
        await self._conn.execute(
            "UPDATE raw_artifacts SET file_path = ? WHERE id = ?",
            (file_path, artifact_id),
        )
        await self._conn.commit()

    async def create_receipt(
        self, receipt: IngestionReceipt, *, commit: bool = True
    ) -> IngestionReceipt:
        await self._conn.execute(
            """INSERT OR IGNORE INTO ingestion_receipts (
                id, source, received_at, observed_at, status, artifact_id,
                device_id, area_id, external_id, metadata, event_id,
                evidence_id, episode_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                receipt.id,
                receipt.source,
                receipt.received_at.isoformat(),
                receipt.observed_at.isoformat() if receipt.observed_at else None,
                receipt.status.value,
                receipt.artifact_id,
                receipt.device_id,
                receipt.area_id,
                receipt.external_id,
                json.dumps(receipt.metadata),
                receipt.event_id,
                receipt.evidence_id,
                receipt.episode_id,
            ),
        )
        if commit:
            await self._conn.commit()
        return receipt

    async def get_receipt(self, receipt_id: str) -> IngestionReceipt | None:
        rows = await self._conn.execute_fetchall(
            "SELECT * FROM ingestion_receipts WHERE id = ?", (receipt_id,)
        )
        return self._row_to_receipt(rows[0]) if rows else None

    async def update_receipt(
        self,
        receipt_id: str,
        *,
        status: ReceiptStatus,
        observed_at: datetime | None,
        device_id: str,
        area_id: str,
        external_id: str | None,
        metadata: dict,
    ) -> None:
        await self._conn.execute(
            """UPDATE ingestion_receipts
               SET status = ?, observed_at = ?, device_id = ?, area_id = ?,
                   external_id = ?, metadata = ?
               WHERE id = ?""",
            (
                status.value,
                observed_at.isoformat() if observed_at else None,
                device_id,
                area_id,
                external_id,
                json.dumps(metadata),
                receipt_id,
            ),
        )
        await self._conn.commit()

    async def list_receipts(
        self,
        *,
        episode_id: str | None = None,
        event_id: str | None = None,
        evidence_id: str | None = None,
        source: str | None = None,
        status: ReceiptStatus | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[IngestionReceipt]:
        clauses: list[str] = []
        params: list[object] = []
        for column, value in (
            ("episode_id", episode_id),
            ("event_id", event_id),
            ("evidence_id", evidence_id),
            ("source", source),
            ("status", status.value if status else None),
        ):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = await self._conn.execute_fetchall(
            f"""SELECT * FROM ingestion_receipts{where}
                ORDER BY received_at ASC, id ASC LIMIT ? OFFSET ?""",
            [*params, limit, offset],
        )
        return [self._row_to_receipt(row) for row in rows]

    async def link_receipt(
        self,
        receipt_id: str,
        *,
        event_id: str | None = None,
        evidence_id: str | None = None,
        episode_id: str | None = None,
    ) -> None:
        updates: list[str] = []
        params: list[object] = []
        for column, value in (
            ("event_id", event_id),
            ("evidence_id", evidence_id),
            ("episode_id", episode_id),
        ):
            if value is not None:
                updates.append(f"{column} = ?")
                params.append(value)
        if not updates:
            return
        params.append(receipt_id)
        await self._conn.execute(
            f"UPDATE ingestion_receipts SET {', '.join(updates)} WHERE id = ?", params
        )
        await self._conn.commit()

    @staticmethod
    def _row_to_artifact(row: aiosqlite.Row) -> RawArtifact:
        return RawArtifact(
            id=row["id"],
            artifact_type=row["artifact_type"],
            file_path=row["file_path"],
            mime_type=row["mime_type"],
            byte_size=row["byte_size"],
            sha256=row["sha256"],
            created_at=datetime.fromisoformat(row["created_at"]),
            original_filename=row["original_filename"],
            sealed=bool(row["sealed"]),
            metadata=json.loads(row["metadata"]),
        )

    @staticmethod
    def _row_to_receipt(row: aiosqlite.Row) -> IngestionReceipt:
        return IngestionReceipt(
            id=row["id"],
            source=row["source"],
            received_at=datetime.fromisoformat(row["received_at"]),
            observed_at=datetime.fromisoformat(row["observed_at"]) if row["observed_at"] else None,
            status=ReceiptStatus(row["status"]),
            artifact_id=row["artifact_id"],
            device_id=row["device_id"],
            area_id=row["area_id"],
            external_id=row["external_id"],
            metadata=json.loads(row["metadata"]),
            event_id=row["event_id"],
            evidence_id=row["evidence_id"],
            episode_id=row["episode_id"],
        )
