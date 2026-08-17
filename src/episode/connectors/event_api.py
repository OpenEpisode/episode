from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, field_validator

from episode.domain.models import ReceiptStatus
from episode.ingestion.models import (
    EventObservation,
    IngressDelivery,
    IngressHandlerResult,
    StoredIngressEnvelope,
)
from episode.ingestion.router import IngressHandlerRegistration, IngressRouter
from episode.ingestion.service import IngestionService

logger = logging.getLogger(__name__)

CONNECTOR_TYPE = "event_api"
HANDLER_ID = "core:normalized-event"
DEFAULT_PATH = "/api/v1/events"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")


class _DuplicateKeyError(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


class NormalizedEventPayload(BaseModel):
    """The stable, vendor-neutral contract accepted by the Event API."""

    model_config = ConfigDict(extra="forbid")

    device_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER.pattern)
    event_type: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER.pattern)
    event_state: str = Field(default="active", pattern="^(active|inactive)$")
    timestamp: AwareDatetime | None = None
    source: str = Field(
        default="external", min_length=1, max_length=64, pattern=_IDENTIFIER.pattern
    )
    external_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=_IDENTIFIER.pattern,
    )
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timestamp", mode="before")
    @classmethod
    def require_iso_timestamp(cls, value: object) -> object:
        if value is not None and not isinstance(value, str):
            raise ValueError("timestamp must be an ISO 8601 string with a timezone")
        return value


def _validation_errors(exc: ValidationError) -> list[dict[str, object]]:
    return [
        {
            "field": ".".join(str(item) for item in error["loc"]),
            "type": error["type"],
            "message": error["msg"],
        }
        for error in exc.errors(include_input=False, include_url=False)
    ]


def _external_dedup_key(source: str, device_id: str, external_id: str) -> str:
    value = "\x1f".join(("event-api", source, device_id, external_id))
    return sha256(value.encode()).hexdigest()


class EventAPIConnector:
    """Accept normalized JSON Events through the shared raw-first boundary."""

    def __init__(
        self,
        name: str,
        ingestion: IngestionService,
        router: IngressRouter,
        config: dict,
        api_port: int,
    ) -> None:
        self.name = name
        self._ingestion = ingestion
        self._router = router
        self._path = config.get("path", DEFAULT_PATH)
        self._api_port = api_port
        self._max_payload_bytes = int(config.get("max_payload_bytes", 64 * 1024))
        if not isinstance(self._path, str) or not self._path.startswith("/"):
            raise ValueError("Event API path must start with '/'")
        if self._max_payload_bytes <= 0:
            raise ValueError("Event API max_payload_bytes must be greater than zero")
        self._running = False
        self._request_count = 0
        self._accepted_count = 0
        self._duplicate_count = 0
        self._rejected_count = 0
        self._unmatched_count = 0
        self._last_event_at: datetime | None = None

    def status(self) -> dict[str, object]:
        handler = self._router.status(HANDLER_ID) or {}
        return {
            "name": self.name,
            "type": CONNECTOR_TYPE,
            "running": self._running,
            "path": self._path,
            "port": self._api_port,
            "requests_handled": self._request_count,
            "events_accepted": self._accepted_count,
            "duplicates": self._duplicate_count,
            "requests_rejected": self._rejected_count,
            "unmatched": self._unmatched_count,
            "last_event_at": self._last_event_at,
            "handler_failures": int(handler.get("failures", 0)),
            "handler_timeouts": int(handler.get("timeouts", 0)),
            "last_error": handler.get("last_error"),
        }

    async def _read_body(self, request: Request) -> bytes:
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                length = int(content_length)
            except ValueError:
                raise HTTPException(400, "Invalid Content-Length header") from None
            if length < 0:
                raise HTTPException(400, "Invalid Content-Length header")
            if length > self._max_payload_bytes:
                raise HTTPException(413, "Event API payload is too large")

        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > self._max_payload_bytes:
                raise HTTPException(413, "Event API payload is too large")
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _matches(envelope: StoredIngressEnvelope) -> bool:
        return (
            envelope.transport == "http"
            and envelope.metadata.get("connector_type") == CONNECTOR_TYPE
        )

    @staticmethod
    async def _handle(envelope: StoredIngressEnvelope) -> IngressHandlerResult:
        media_type = envelope.media_type.partition(";")[0].strip().lower()
        if media_type != "application/json" and not media_type.endswith("+json"):
            return IngressHandlerResult(
                claimed=True,
                status=ReceiptStatus.REJECTED,
                metadata={
                    "reason": "unsupported_media_type",
                    "message": "Event API requests must use application/json.",
                },
            )

        try:
            document = json.loads(envelope.payload, object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateKeyError) as exc:
            return IngressHandlerResult(
                claimed=True,
                status=ReceiptStatus.REJECTED,
                metadata={
                    "reason": "invalid_json",
                    "message": str(exc),
                },
            )
        if not isinstance(document, dict):
            return IngressHandlerResult(
                claimed=True,
                status=ReceiptStatus.REJECTED,
                metadata={
                    "reason": "invalid_event",
                    "message": "Event API payload must be a JSON object.",
                },
            )

        try:
            payload = NormalizedEventPayload.model_validate(document)
        except ValidationError as exc:
            return IngressHandlerResult(
                claimed=True,
                status=ReceiptStatus.REJECTED,
                metadata={
                    "reason": "invalid_event",
                    "validation_errors": _validation_errors(exc),
                },
            )

        header_external_id = envelope.metadata.get("idempotency_key")
        if header_external_id is not None and (
            not isinstance(header_external_id, str)
            or not (1 <= len(header_external_id) <= 128)
            or not _IDENTIFIER.fullmatch(header_external_id)
        ):
            return IngressHandlerResult(
                claimed=True,
                status=ReceiptStatus.REJECTED,
                metadata={
                    "reason": "invalid_idempotency_key",
                    "message": "Idempotency-Key must be a safe identifier of 1 to 128 characters.",
                },
            )
        if payload.external_id and header_external_id and payload.external_id != header_external_id:
            return IngressHandlerResult(
                claimed=True,
                status=ReceiptStatus.REJECTED,
                metadata={
                    "reason": "conflicting_idempotency_key",
                    "message": "external_id and Idempotency-Key must match when both are provided.",
                },
            )

        external_id = payload.external_id or header_external_id
        timestamp = payload.timestamp or envelope.received_at
        source = f"event-api:{payload.source}"
        dedup_key = (
            _external_dedup_key(payload.source, payload.device_id, external_id)
            if external_id
            else ""
        )
        return IngressHandlerResult(
            claimed=True,
            external_id=external_id,
            event=EventObservation(
                device_id=payload.device_id,
                timestamp=timestamp,
                event_type=payload.event_type,
                event_state=payload.event_state,
                source=source,
                dedup_key=dedup_key,
                metadata={
                    **payload.metadata,
                    "external_source": payload.source,
                    **({"external_id": external_id} if external_id else {}),
                },
            ),
            metadata={"external_source": payload.source},
        )

    def mount(self, app: FastAPI) -> None:
        path = self._path

        @app.post(
            path,
            summary="Submit a normalized Event",
            tags=["Event input"],
            openapi_extra={
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {"schema": NormalizedEventPayload.model_json_schema()}
                    },
                }
            },
        )
        async def submit_event(request: Request):
            self._request_count += 1
            try:
                body = await self._read_body(request)
                client = request.client.host if request.client else ""
                outcome = await self._ingestion.accept(
                    IngressDelivery(
                        source="http:event_api",
                        transport="http",
                        received_at=datetime.now(tz=timezone.utc),
                        payload=body,
                        media_type=request.headers.get("content-type", "application/octet-stream"),
                        metadata={
                            "connector_type": CONNECTOR_TYPE,
                            "path": path,
                            "client_ip": client,
                            **(
                                {"idempotency_key": request.headers["idempotency-key"]}
                                if "idempotency-key" in request.headers
                                else {}
                            ),
                        },
                    )
                )
            except HTTPException:
                self._rejected_count += 1
                raise
            except Exception as exc:
                self._rejected_count += 1
                logger.exception("%s: failed to preserve Event API delivery", self.name)
                raise HTTPException(503, "Event API delivery could not be preserved") from exc

            receipt = outcome.receipt
            canonical = outcome.canonical_event
            response = {
                "status": receipt.status.value,
                "receipt_id": receipt.id,
                "event_id": canonical.event.id if canonical else None,
                "episode_id": canonical.event.episode_id if canonical else None,
                "duplicate": bool(canonical and not canonical.created),
                "reason": receipt.metadata.get("reason"),
                "message": receipt.metadata.get("message"),
                "validation_errors": receipt.metadata.get("validation_errors", []),
            }
            if receipt.status == ReceiptStatus.REJECTED:
                self._rejected_count += 1
                return JSONResponse(status_code=422, content=response)
            if receipt.status == ReceiptStatus.UNMATCHED:
                self._unmatched_count += 1
                return JSONResponse(status_code=422, content=response)
            if canonical is None:
                self._rejected_count += 1
                return JSONResponse(status_code=500, content=response)

            self._last_event_at = datetime.now(tz=timezone.utc)
            if canonical.created:
                self._accepted_count += 1
                return JSONResponse(status_code=201, content=response)
            self._duplicate_count += 1
            return JSONResponse(status_code=200, content=response)

        logger.info("%s: mounted at %s", self.name, path)

    async def start(self) -> None:
        if self._running:
            return
        self._router.register(
            IngressHandlerRegistration(
                id=HANDLER_ID,
                matcher=self._matches,
                handler=self._handle,
            )
        )
        self._running = True

    async def stop(self) -> None:
        if not self._running:
            return
        self._router.unregister(HANDLER_ID)
        self._running = False
