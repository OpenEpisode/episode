from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from episode.ingestion.models import IngressDelivery
from episode.ingestion.service import IngestionService

logger = logging.getLogger(__name__)


class HTTPIngressResponse(BaseModel):
    status: str
    receipt_id: str


class HTTPIngressConnector:
    """Receive an opaque HTTP payload and hand it to the raw-first boundary."""

    def __init__(
        self,
        name: str,
        ingestion: IngestionService,
        config: dict,
        api_port: int,
        *,
        connector_type: str,
    ):
        self.name = name
        self._ingestion = ingestion
        self._connector_type = connector_type
        self._device_id = config.get("device_id", "")
        self._area_id = config.get("area_id", "")
        self._path = config.get("path", "/alarm")
        self._api_port = api_port
        self._max_payload_bytes = int(config.get("max_payload_bytes", 16 * 1024 * 1024))
        if not self._connector_type:
            raise ValueError("HTTP ingress connector_type is required")
        if not isinstance(self._path, str) or not self._path.startswith("/"):
            raise ValueError("HTTP ingress path must start with '/'")
        if self._max_payload_bytes <= 0:
            raise ValueError("HTTP ingress max_payload_bytes must be greater than zero")
        self._running = False
        self._request_count = 0
        self._rejected_count = 0

    def status(self) -> dict:
        return {
            "name": self.name,
            "type": self._connector_type,
            "running": self._running,
            "path": self._path,
            "port": self._api_port,
            "requests_handled": self._request_count,
            "requests_rejected": self._rejected_count,
        }

    async def _read_body(self, request: Request) -> bytes:
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > self._max_payload_bytes:
                    raise HTTPException(413, "HTTP ingress payload is too large")
            except ValueError:
                raise HTTPException(400, "Invalid Content-Length header") from None

        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > self._max_payload_bytes:
                raise HTTPException(413, "HTTP ingress payload is too large")
            chunks.append(chunk)
        return b"".join(chunks)

    def mount(self, app: FastAPI) -> None:
        path = self._path

        @app.post(path, response_model=HTTPIngressResponse)
        async def handle_delivery(request: Request):
            self._request_count += 1
            try:
                body = await self._read_body(request)
                client = request.client.host if request.client else ""
                outcome = await self._ingestion.accept(
                    IngressDelivery(
                        source=f"http:{self._connector_type}",
                        transport="http",
                        received_at=datetime.now(tz=timezone.utc),
                        payload=body,
                        media_type=request.headers.get("content-type", "application/octet-stream"),
                        device_id=self._device_id,
                        area_id=self._area_id,
                        metadata={
                            "connector_type": self._connector_type,
                            "path": path,
                            "client_ip": client,
                        },
                    )
                )
            except HTTPException:
                self._rejected_count += 1
                raise
            except Exception as exc:
                self._rejected_count += 1
                logger.exception("%s: failed to preserve HTTP delivery", self.name)
                raise HTTPException(503, "HTTP delivery could not be preserved") from exc
            return {"status": "ok", "receipt_id": outcome.receipt.id}

        logger.info("%s: mounted at %s", self.name, path)

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False
