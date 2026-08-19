from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from episode.ingestion.models import IngressHandlerResult, StoredIngressEnvelope

logger = logging.getLogger(__name__)

IngressMatcher = Callable[[StoredIngressEnvelope], bool]
IngressHandler = Callable[[StoredIngressEnvelope], Awaitable[IngressHandlerResult]]


@dataclass(frozen=True)
class IngressHandlerRegistration:
    id: str
    handler: IngressHandler
    matcher: IngressMatcher
    timeout: float = 5.0

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("Ingress handler id is required")
        if self.timeout <= 0:
            raise ValueError("Ingress handler timeout must be greater than zero")


@dataclass(frozen=True)
class IngressDispatchResult:
    handler_id: str
    state: str
    result: IngressHandlerResult | None = None
    error: str | None = None


@dataclass
class _HandlerMetrics:
    deliveries: int = 0
    claimed: int = 0
    failures: int = 0
    timeouts: int = 0
    last_delivery_at: datetime | None = None
    last_error: str | None = None


class IngressRouter:
    """Dispatch preserved deliveries to explicitly registered ingress handlers."""

    def __init__(self) -> None:
        self._registrations: dict[str, IngressHandlerRegistration] = {}
        self._metrics: dict[str, _HandlerMetrics] = {}

    def register(self, registration: IngressHandlerRegistration) -> None:
        if registration.id in self._registrations:
            raise ValueError(f"Ingress handler {registration.id!r} is already registered")
        self._registrations[registration.id] = registration
        self._metrics[registration.id] = _HandlerMetrics()

    def unregister(self, handler_id: str) -> None:
        self._registrations.pop(handler_id, None)
        self._metrics.pop(handler_id, None)

    async def dispatch(self, envelope: StoredIngressEnvelope) -> tuple[IngressDispatchResult, ...]:
        matched: list[IngressHandlerRegistration] = []
        failures: list[IngressDispatchResult] = []
        for registration in self._registrations.values():
            try:
                if registration.matcher(envelope):
                    matched.append(registration)
            except (Exception, SystemExit):
                logger.exception(
                    "Ingress matcher %s failed for receipt %s",
                    registration.id,
                    envelope.receipt_id,
                )
                metrics = self._metrics[registration.id]
                metrics.failures += 1
                metrics.last_error = "Matcher failed."
                failures.append(
                    IngressDispatchResult(
                        handler_id=registration.id,
                        state="failed",
                        error="Matcher failed.",
                    )
                )
        if not matched:
            return tuple(failures)

        results = await asyncio.gather(
            *(self._invoke(registration, envelope) for registration in matched)
        )
        return (*failures, *results)

    async def _invoke(
        self,
        registration: IngressHandlerRegistration,
        envelope: StoredIngressEnvelope,
    ) -> IngressDispatchResult:
        metrics = self._metrics[registration.id]
        metrics.deliveries += 1
        metrics.last_delivery_at = datetime.now(tz=timezone.utc)
        try:
            result = await asyncio.wait_for(
                registration.handler(envelope),
                timeout=registration.timeout,
            )
            if not isinstance(result, IngressHandlerResult):
                raise TypeError("Ingress handler must return IngressHandlerResult")
        except TimeoutError:
            metrics.timeouts += 1
            metrics.last_error = "Handler timed out."
            logger.warning(
                "Ingress handler %s timed out for receipt %s",
                registration.id,
                envelope.receipt_id,
            )
            return IngressDispatchResult(
                handler_id=registration.id,
                state="timed_out",
                error="Handler timed out.",
            )
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            metrics.failures += 1
            metrics.last_error = "Handler cancelled itself."
            logger.warning(
                "Ingress handler %s cancelled itself for receipt %s",
                registration.id,
                envelope.receipt_id,
            )
            return IngressDispatchResult(
                handler_id=registration.id,
                state="failed",
                error="Handler cancelled itself.",
            )
        except (Exception, SystemExit):
            metrics.failures += 1
            metrics.last_error = "Handler failed."
            logger.exception(
                "Ingress handler %s failed for receipt %s",
                registration.id,
                envelope.receipt_id,
            )
            return IngressDispatchResult(
                handler_id=registration.id,
                state="failed",
                error="Handler failed.",
            )

        if result.claimed:
            metrics.claimed += 1
        metrics.last_error = None
        return IngressDispatchResult(
            handler_id=registration.id,
            state="claimed" if result.claimed else "observed",
            result=result,
        )

    def status(self, handler_id: str) -> dict[str, object] | None:
        metrics = self._metrics.get(handler_id)
        if metrics is None:
            return None
        return {
            "deliveries": metrics.deliveries,
            "claimed": metrics.claimed,
            "failures": metrics.failures,
            "timeouts": metrics.timeouts,
            "last_delivery_at": metrics.last_delivery_at,
            "last_error": metrics.last_error,
        }
