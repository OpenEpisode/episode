from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from episode.config import EpisodeConfig
from episode.domain.models import IngestionReceipt
from episode.storage.files import describe_artifact
from episode.storage.repository import SQLITE_BUSY_TIMEOUT_MS, Repository


async def _repository(tmp_path) -> Repository:
    repository = Repository(
        EpisodeConfig(
            data_dir=str(tmp_path),
            db_path=str(tmp_path / "episode.db"),
        )
    )
    await repository.initialize()
    return repository


def _delivery(tmp_path, name: str):
    path = tmp_path / name
    path.write_bytes(name.encode())
    return (
        describe_artifact(str(path), "event_payload", "application/octet-stream"),
        IngestionReceipt(
            source="test:connector",
            received_at=datetime.now(tz=timezone.utc),
        ),
    )


@pytest.mark.asyncio
async def test_repository_uses_wal_for_delivery_and_main_connection_concurrency(tmp_path):
    repository = await _repository(tmp_path)
    try:
        main_mode = await repository._conn.execute_fetchall("PRAGMA journal_mode")
        delivery_mode = await repository._delivery_conn.execute_fetchall("PRAGMA journal_mode")
        main_timeout = await repository._conn.execute_fetchall("PRAGMA busy_timeout")
        delivery_timeout = await repository._delivery_conn.execute_fetchall("PRAGMA busy_timeout")
        main_foreign_keys = await repository._conn.execute_fetchall("PRAGMA foreign_keys")

        assert main_mode[0][0].lower() == "wal"
        assert delivery_mode[0][0].lower() == "wal"
        assert main_timeout[0][0] == SQLITE_BUSY_TIMEOUT_MS
        assert delivery_timeout[0][0] == SQLITE_BUSY_TIMEOUT_MS
        assert main_foreign_keys[0][0] == 1

        # A long-lived read on the main connection must not prevent the
        # dedicated delivery connection from committing immutable input.
        await repository._conn.execute("BEGIN")
        await repository._conn.execute_fetchall("SELECT * FROM episodes")
        artifact, receipt = _delivery(tmp_path, "concurrent.bin")
        stored, _ = await repository.persist_delivery(artifact, receipt)
        assert stored.id == artifact.id
        await repository._conn.rollback()
    finally:
        await repository.close()


@pytest.mark.asyncio
async def test_cancelled_delivery_rolls_back_before_releasing_lock(tmp_path, monkeypatch):
    repository = await _repository(tmp_path)
    try:
        assert repository._delivery_provenance is not None
        assert repository._delivery_conn is not None
        original_create = repository._delivery_provenance.create_artifact
        inserted = asyncio.Event()
        hold_open = asyncio.Event()

        async def create_then_wait(artifact, *, commit=True):
            stored = await original_create(artifact, commit=commit)
            inserted.set()
            await hold_open.wait()
            return stored

        monkeypatch.setattr(
            repository._delivery_provenance,
            "create_artifact",
            create_then_wait,
        )
        artifact, receipt = _delivery(tmp_path, "cancelled.bin")
        task = asyncio.create_task(repository.persist_delivery(artifact, receipt))
        await inserted.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert repository._delivery_conn.in_transaction is False

        monkeypatch.setattr(
            repository._delivery_provenance,
            "create_artifact",
            original_create,
        )
        next_artifact, next_receipt = _delivery(tmp_path, "after-cancellation.bin")
        stored, accepted = await repository.persist_delivery(next_artifact, next_receipt)
        assert stored.id == next_artifact.id
        assert accepted.artifact_id == stored.id
    finally:
        await repository.close()
