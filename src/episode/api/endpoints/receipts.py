from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from episode.api.context import ApiContext
from episode.api.errors import PUBLIC_ERROR_RESPONSES
from episode.api.pagination import DEFAULT_LIMIT, PageLimit, PageOffset
from episode.api.projections import public_receipt
from episode.api.schemas import IngestionReceiptResponse
from episode.domain.models import ReceiptStatus


def receipts_router(context: ApiContext) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/receipts",
        tags=["receipts"],
        responses=PUBLIC_ERROR_RESPONSES,
    )
    repo = context.repository

    @router.get("", response_model=list[IngestionReceiptResponse])
    async def list_receipts(
        episode_id: str | None = None,
        event_id: str | None = None,
        evidence_id: str | None = None,
        source: str | None = None,
        status: ReceiptStatus | None = None,
        limit: PageLimit = DEFAULT_LIMIT,
        offset: PageOffset = 0,
    ):
        receipts = await repo.list_ingestion_receipts(
            episode_id=episode_id,
            event_id=event_id,
            evidence_id=evidence_id,
            source=source,
            status=status,
            limit=limit,
            offset=offset,
        )
        return [public_receipt(receipt) for receipt in receipts]

    @router.get("/{receipt_id}", response_model=IngestionReceiptResponse)
    async def get_receipt(receipt_id: str):
        receipt = await repo.get_ingestion_receipt(receipt_id)
        if receipt is None:
            raise HTTPException(404, "Receipt not found")
        return public_receipt(receipt)

    @router.get("/{receipt_id}/artifact")
    async def receipt_artifact(receipt_id: str):
        receipt = await repo.get_ingestion_receipt(receipt_id)
        if not receipt or not receipt.artifact_id:
            raise HTTPException(404, "Receipt artifact not found")
        artifact = await repo.get_raw_artifact(receipt.artifact_id)
        if not artifact or not os.path.isfile(artifact.file_path):
            raise HTTPException(404, "Receipt artifact not found")
        return FileResponse(
            artifact.file_path,
            media_type=artifact.mime_type,
            filename=artifact.original_filename or os.path.basename(artifact.file_path),
        )

    return router
