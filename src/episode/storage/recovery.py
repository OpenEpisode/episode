from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import aiosqlite

from episode.storage.files import async_move_to_episode, sha256_file
from episode.storage.provenance import ProvenanceStore

logger = logging.getLogger(__name__)


async def reconcile_episode_counts(connection: aiosqlite.Connection) -> int:
    """Repair denormalized Episode counters from their canonical rows."""

    cursor = await connection.execute(
        """UPDATE episodes
           SET event_count = (
                   SELECT COUNT(*) FROM events WHERE events.episode_id = episodes.id
               ),
               evidence_count = (
                   SELECT COUNT(*) FROM evidence WHERE evidence.episode_id = episodes.id
               )
           WHERE event_count != (
                   SELECT COUNT(*) FROM events WHERE events.episode_id = episodes.id
               )
              OR evidence_count != (
                   SELECT COUNT(*) FROM evidence WHERE evidence.episode_id = episodes.id
               )"""
    )
    await connection.commit()
    repaired = max(cursor.rowcount, 0)
    if repaired:
        logger.info("Reconciled counters for %s Episode(s)", repaired)
    return repaired


def _evidence_subdir(evidence_type: str) -> str:
    return {
        "snapshot": "snapshots",
        "recording": "recordings",
    }.get(evidence_type, "other")


def _artifact_subdir(
    artifact_type: str,
    *,
    event_id: str | None,
    evidence_type: str | None,
) -> str:
    if event_id:
        return "events"
    if evidence_type:
        return _evidence_subdir(evidence_type)
    return {
        "event_payload": "events",
        "snapshot": "snapshots",
        "recording": "recordings",
    }.get(artifact_type, "other")


def _checksum_matches(path: Path, checksum: str) -> bool:
    return not checksum or sha256_file(str(path)) == checksum


def _find_relocated_file(
    target_dir: Path,
    recorded_path: str,
    checksum: str,
) -> str | None:
    if not target_dir.is_dir():
        return None
    basename = os.path.basename(recorded_path)
    exact = target_dir / basename
    if exact.is_file() and _checksum_matches(exact, checksum):
        return str(exact)
    if not checksum:
        return None
    for candidate in target_dir.iterdir():
        if candidate.is_file() and _checksum_matches(candidate, checksum):
            return str(candidate)
    return None


async def _recover_path(
    data_dir: str,
    episode_id: str,
    subdir: str,
    recorded_path: str,
    checksum: str = "",
) -> str | None:
    if not recorded_path:
        return None
    target_dir = Path(data_dir) / "episodes" / episode_id / subdir
    current = Path(recorded_path)
    if current.is_file():
        if current.parent == target_dir:
            return str(current)
        return await async_move_to_episode(data_dir, episode_id, str(current), subdir)
    return await asyncio.to_thread(
        _find_relocated_file,
        target_dir,
        recorded_path,
        checksum,
    )


async def reconcile_episode_paths(
    connection: aiosqlite.Connection,
    provenance: ProvenanceStore,
    data_dir: str,
) -> int:
    """Finish interrupted moves into portable Episode folders."""

    repaired = 0
    artifact_rows = await connection.execute_fetchall(
        """SELECT a.id, a.artifact_type, a.file_path, a.sha256,
                  r.id AS receipt_id, r.event_id, r.evidence_id,
                  r.episode_id AS receipt_episode_id, e.evidence_type,
                  COALESCE(r.episode_id, ev.episode_id, e.episode_id) AS episode_id
           FROM raw_artifacts a
           JOIN ingestion_receipts r ON r.artifact_id = a.id
           LEFT JOIN events ev ON ev.id = r.event_id
           LEFT JOIN evidence e ON e.id = r.evidence_id
           WHERE COALESCE(r.episode_id, ev.episode_id, e.episode_id) IS NOT NULL
           ORDER BY r.received_at ASC"""
    )
    repaired_artifacts: set[str] = set()
    for row in artifact_rows:
        if not row["receipt_episode_id"]:
            await provenance.link_receipt(
                row["receipt_id"],
                event_id=row["event_id"],
                evidence_id=row["evidence_id"],
                episode_id=row["episode_id"],
            )
            repaired += 1

        if row["id"] in repaired_artifacts:
            continue
        subdir = _artifact_subdir(
            row["artifact_type"],
            event_id=row["event_id"],
            evidence_type=row["evidence_type"],
        )
        recovered = await _recover_path(
            data_dir,
            row["episode_id"],
            subdir,
            row["file_path"],
            row["sha256"],
        )
        if recovered and recovered != row["file_path"]:
            await provenance.update_artifact_path(row["id"], recovered)
            repaired += 1
        repaired_artifacts.add(row["id"])

    evidence_rows = await connection.execute_fetchall(
        """SELECT e.id, e.episode_id, e.evidence_type, e.file_path,
                  e.sha256, e.artifact_id, a.file_path AS artifact_path,
                  a.sha256 AS artifact_sha256
           FROM evidence e
           LEFT JOIN raw_artifacts a ON a.id = e.artifact_id
           WHERE e.episode_id IS NOT NULL"""
    )
    for row in evidence_rows:
        recorded = row["artifact_path"] or row["file_path"]
        checksum = row["artifact_sha256"] or row["sha256"] or ""
        recovered = await _recover_path(
            data_dir,
            row["episode_id"],
            _evidence_subdir(row["evidence_type"]),
            recorded,
            checksum,
        )
        if not recovered:
            logger.warning(
                "Evidence %s points to a missing file and could not be reconciled",
                row["id"],
            )
            continue
        if row["artifact_id"] and recovered != row["artifact_path"]:
            await provenance.update_artifact_path(row["artifact_id"], recovered)
        if recovered != row["file_path"]:
            await connection.execute(
                "UPDATE evidence SET file_path = ? WHERE id = ?",
                (recovered, row["id"]),
            )
            repaired += 1

    event_rows = await connection.execute_fetchall(
        """SELECT id, episode_id, raw_payload_path
           FROM events
           WHERE episode_id IS NOT NULL
             AND raw_payload_path IS NOT NULL
             AND raw_payload_path != ''"""
    )
    for row in event_rows:
        receipt_artifacts = await connection.execute_fetchall(
            """SELECT a.id, a.file_path, a.sha256
               FROM ingestion_receipts r
               JOIN raw_artifacts a ON a.id = r.artifact_id
               WHERE r.event_id = ?
               ORDER BY r.received_at ASC""",
            (row["id"],),
        )
        recorded = row["raw_payload_path"]
        selected = next(
            (
                artifact
                for artifact in receipt_artifacts
                if os.path.basename(artifact["file_path"]) == os.path.basename(recorded)
            ),
            receipt_artifacts[0] if receipt_artifacts else None,
        )
        checksum = selected["sha256"] if selected else ""
        recovered = await _recover_path(
            data_dir,
            row["episode_id"],
            "events",
            recorded,
            checksum,
        )
        if not recovered and selected:
            recovered = await _recover_path(
                data_dir,
                row["episode_id"],
                "events",
                selected["file_path"],
                checksum,
            )
        if not recovered:
            logger.warning(
                "Event %s points to a missing payload and could not be reconciled",
                row["id"],
            )
            continue
        if selected and recovered != selected["file_path"]:
            await provenance.update_artifact_path(selected["id"], recovered)
        if recovered != recorded:
            await connection.execute(
                "UPDATE events SET raw_payload_path = ? WHERE id = ?",
                (recovered, row["id"]),
            )
            repaired += 1

    await connection.commit()
    if repaired:
        logger.info("Reconciled %s interrupted Episode file operations", repaired)
    return repaired
