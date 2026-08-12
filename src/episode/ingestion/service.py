from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path

from episode.domain.models import Event, Evidence, IngestionReceipt, RawArtifact, ReceiptStatus
from episode.engine.engine import CanonicalEventResult, EpisodeEngine
from episode.ingestion.models import FileIngressDelivery, IngressDelivery, StoredIngressEnvelope
from episode.ingestion.router import IngressDispatchResult, IngressRouter
from episode.storage.files import describe_artifact, move_received_file, save_bytes
from episode.storage.repository import Repository

logger = logging.getLogger(__name__)

_SAFE_COMPONENT = re.compile(r"[^a-zA-Z0-9_.-]+")


def _safe_component(value: str, fallback: str) -> str:
    component = _SAFE_COMPONENT.sub("-", value).strip(".-")
    return component[:80] or fallback


def _extension(media_type: str, original_filename: str | None) -> str:
    if original_filename:
        suffix = Path(original_filename).suffix
        if suffix and len(suffix) <= 12:
            return suffix
    if media_type in {"application/xml", "text/xml"}:
        return ".xml"
    guessed = mimetypes.guess_extension(media_type, strict=False)
    return guessed if guessed and len(guessed) <= 12 else ".bin"


@dataclass(frozen=True)
class IngestionOutcome:
    receipt: IngestionReceipt
    dispatches: tuple[IngressDispatchResult, ...]
    canonical_event: CanonicalEventResult | None = None
    evidence: Evidence | None = None


class IngestionService:
    """Core-owned raw-first delivery preservation and interpretation pipeline."""

    def __init__(
        self,
        data_dir: str,
        repository: Repository,
        engine: EpisodeEngine,
        router: IngressRouter,
        *,
        max_payload_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        self._data_dir = data_dir
        self._repository = repository
        self._engine = engine
        self._router = router
        self._max_payload_bytes = max_payload_bytes

    async def accept(self, delivery: IngressDelivery) -> IngestionOutcome:
        self._validate_identity(delivery.source, delivery.transport)
        if len(delivery.payload) > self._max_payload_bytes:
            raise ValueError("Ingress delivery exceeds the configured safety limit")

        transport = _safe_component(delivery.transport, "unknown")
        source = _safe_component(delivery.source, "delivery")
        path = await asyncio.to_thread(
            save_bytes,
            self._data_dir,
            f"orphans/ingress/{transport}",
            delivery.payload,
            prefix=source,
            extension=_extension(delivery.media_type, delivery.original_filename),
        )
        artifact = await asyncio.to_thread(
            describe_artifact,
            path,
            delivery.artifact_type,
            delivery.media_type,
            original_filename=delivery.original_filename,
            metadata={
                "transport": delivery.transport,
                "source": delivery.source,
                **dict(delivery.metadata),
            },
        )
        receipt = IngestionReceipt(
            source=delivery.source,
            received_at=delivery.received_at,
            status=ReceiptStatus.ACCEPTED,
            device_id=delivery.device_id,
            area_id=delivery.area_id,
            metadata={
                "transport": delivery.transport,
                **dict(delivery.metadata),
            },
        )

        # This durable boundary always completes before a plugin sees the payload.
        artifact, receipt = await self._repository.persist_delivery(artifact, receipt)
        return await self._interpret(delivery, artifact, receipt, delivery.payload)

    async def accept_file(self, delivery: FileIngressDelivery) -> IngestionOutcome:
        """Preserve an uploaded file before exposing its bytes to plugin handlers."""
        self._validate_identity(delivery.source, delivery.transport)
        if not delivery.file_path.is_file():
            raise ValueError("Ingress delivery file does not exist")
        byte_size = delivery.file_path.stat().st_size
        if byte_size > self._max_payload_bytes:
            raise ValueError("Ingress delivery exceeds the configured safety limit")

        transport = _safe_component(delivery.transport, "unknown")
        original_filename = delivery.original_filename or delivery.file_path.name
        path = await asyncio.to_thread(
            move_received_file,
            self._data_dir,
            str(delivery.file_path),
            f"orphans/ingress/{transport}",
        )
        artifact = await asyncio.to_thread(
            describe_artifact,
            path,
            delivery.artifact_type,
            delivery.media_type,
            original_filename=original_filename,
            metadata={
                "transport": delivery.transport,
                "source": delivery.source,
                **dict(delivery.metadata),
            },
        )
        receipt = IngestionReceipt(
            source=delivery.source,
            received_at=delivery.received_at,
            status=ReceiptStatus.ACCEPTED,
            device_id=delivery.device_id,
            area_id=delivery.area_id,
            metadata={
                "transport": delivery.transport,
                **dict(delivery.metadata),
            },
        )
        artifact, receipt = await self._repository.persist_delivery(artifact, receipt)

        # Reading happens after persistence: plugin code can never observe an
        # upload that has not crossed the raw evidence durability boundary.
        payload = await asyncio.to_thread(Path(path).read_bytes)
        return await self._interpret(delivery, artifact, receipt, payload)

    @staticmethod
    def _validate_identity(source: str, transport: str) -> None:
        if not source:
            raise ValueError("Ingress delivery source is required")
        if not transport:
            raise ValueError("Ingress delivery transport is required")

    async def _interpret(
        self,
        delivery: IngressDelivery | FileIngressDelivery,
        artifact: RawArtifact,
        receipt: IngestionReceipt,
        payload: bytes,
    ) -> IngestionOutcome:
        envelope = StoredIngressEnvelope(
            receipt_id=receipt.id,
            artifact_id=artifact.id,
            source=delivery.source,
            transport=delivery.transport,
            received_at=delivery.received_at,
            payload=payload,
            media_type=delivery.media_type,
            byte_size=artifact.byte_size,
            sha256=artifact.sha256,
            sealed=artifact.sealed,
            device_id=delivery.device_id,
            area_id=delivery.area_id,
            original_filename=delivery.original_filename,
            metadata=delivery.metadata,
        )
        dispatches = await self._router.dispatch(envelope)
        claims = [
            dispatch
            for dispatch in dispatches
            if dispatch.result is not None and dispatch.result.claimed
        ]

        diagnostic_metadata = {
            **receipt.metadata,
            "ingress_handlers": [
                {
                    "id": dispatch.handler_id,
                    "state": dispatch.state,
                    **({"error": dispatch.error} if dispatch.error else {}),
                }
                for dispatch in dispatches
            ],
        }
        if len(claims) > 1:
            diagnostic_metadata["handler_conflict"] = [dispatch.handler_id for dispatch in claims]
            logger.error(
                "Multiple ingress handlers claimed receipt %s: %s",
                receipt.id,
                ", ".join(dispatch.handler_id for dispatch in claims),
            )

        claimed = claims[0] if len(claims) == 1 else None
        handler_result = claimed.result if claimed else None
        status = handler_result.status if handler_result else receipt.status
        if len(claims) > 1:
            status = ReceiptStatus.REJECTED
            diagnostic_metadata["reason"] = "multiple_ingress_handlers_claimed_delivery"
        elif dispatches and any(
            dispatch.state in {"failed", "timed_out"} for dispatch in dispatches
        ):
            status = ReceiptStatus.REJECTED
            diagnostic_metadata["reason"] = "ingress_handler_failed"
        if handler_result:
            diagnostic_metadata.update(dict(handler_result.metadata))

        canonical = None
        evidence = None
        if handler_result and handler_result.event:
            observation = handler_result.event
            receipt.observed_at = observation.timestamp
            device_id = observation.device_id or delivery.device_id
            area_id = observation.area_id or delivery.area_id
            device = await self._repository.get_device(device_id) if device_id else None
            if device is None and observation.device_ip:
                device = await self._repository.find_device_by_ip(observation.device_ip)
            if device:
                device_id = device.id
                area_id = device.area_id
            if not device_id or not area_id:
                status = ReceiptStatus.UNMATCHED
                diagnostic_metadata["reason"] = "device_not_resolved"
            else:
                event = Event(
                    device_id=device_id,
                    area_id=area_id,
                    timestamp=observation.timestamp,
                    event_type=observation.event_type,
                    event_state=observation.event_state,
                    source=observation.source,
                    dedup_key=observation.dedup_key,
                    raw_payload_path=artifact.file_path,
                    metadata={
                        **dict(observation.metadata),
                        "ingress_handler": claimed.handler_id,
                    },
                )
                receipt.device_id = device_id
                receipt.area_id = area_id
                canonical = await self._engine.ingest_event(event, receipt=receipt)
        elif handler_result and handler_result.evidence:
            observation = handler_result.evidence
            receipt.observed_at = observation.timestamp
            device_id = observation.device_id or delivery.device_id
            area_id = observation.area_id or delivery.area_id
            device = await self._repository.get_device(device_id) if device_id else None
            if device is None and observation.device_ip:
                device = await self._repository.find_device_by_ip(observation.device_ip)
            if device:
                device_id = device.id
                area_id = device.area_id
            if not device_id or not area_id:
                status = ReceiptStatus.UNMATCHED
                diagnostic_metadata["reason"] = "device_not_resolved"
            else:
                evidence = Evidence(
                    device_id=device_id,
                    area_id=area_id,
                    timestamp=observation.timestamp,
                    evidence_type=observation.evidence_type,
                    file_path=artifact.file_path,
                    mime_type=observation.mime_type,
                    original_filename=(observation.original_filename or delivery.original_filename),
                    artifact_id=artifact.id,
                    byte_size=artifact.byte_size,
                    sha256=artifact.sha256,
                    metadata={
                        **dict(observation.metadata),
                        "interpretation_source": observation.source,
                        "ingress_handler": claimed.handler_id,
                    },
                )
                receipt.device_id = device_id
                receipt.area_id = area_id
                await self._engine.ingest_evidence(evidence, receipt=receipt)

        receipt.status = status
        receipt.metadata = diagnostic_metadata
        await self._repository.update_ingestion_receipt(
            receipt.id,
            status=status,
            observed_at=receipt.observed_at,
            device_id=receipt.device_id,
            area_id=receipt.area_id,
            metadata=diagnostic_metadata,
        )
        return IngestionOutcome(
            receipt=receipt,
            dispatches=dispatches,
            canonical_event=canonical,
            evidence=evidence,
        )
