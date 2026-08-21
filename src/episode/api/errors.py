from __future__ import annotations

import logging
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from episode.api.schemas import ApiErrorResponse

logger = logging.getLogger(__name__)

_STATUS_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    413: "payload_too_large",
    422: "validation_error",
    429: "rate_limited",
    503: "service_unavailable",
}

PUBLIC_ERROR_RESPONSES = {
    400: {"model": ApiErrorResponse, "description": "Invalid request"},
    404: {"model": ApiErrorResponse, "description": "Resource not found"},
    409: {"model": ApiErrorResponse, "description": "Resource conflict"},
    422: {"model": ApiErrorResponse, "description": "Validation failed"},
    500: {"model": ApiErrorResponse, "description": "Unexpected server error"},
    503: {"model": ApiErrorResponse, "description": "Service unavailable"},
}


def _error_body(code: str, message: str, details: list[dict] | None = None) -> dict:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or [],
        }
    }


def install_error_handlers(app: FastAPI) -> None:
    """Install the stable JSON error envelope used by public API routes."""

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException):
        detail = exc.detail
        message = detail if isinstance(detail, str) else HTTPStatus(exc.status_code).phrase
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(
                _STATUS_CODES.get(exc.status_code, "http_error"),
                message,
            ),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError):
        details = [
            {
                "location": list(error.get("loc", ())),
                "message": error.get("msg", "Invalid value"),
                "type": error.get("type", "validation_error"),
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=_error_body(
                "validation_error",
                "Request validation failed",
                details,
            ),
        )

    @app.exception_handler(Exception)
    async def internal_error(_request: Request, exc: Exception):
        logger.error(
            "Unhandled API error",
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return JSONResponse(
            status_code=500,
            content=_error_body("internal_error", "An unexpected error occurred"),
        )
