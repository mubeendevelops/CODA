"""The exception boundary: turns any handler failure into a well-formed
`Error` (common.proto) with a `Status` classification, rather than letting a
worker crash and leave a message stuck unacknowledged with no diagnosis.

Mirrors architecture.md §2.5's error taxonomy — "the worker classifies; the
orchestrator decides". A handler signals its classification by raising one
of the typed exceptions below; anything else is treated as an unclassified
bug and defaults to RETRYABLE (see `classify_exception`), never a silent
crash and never a guessed OK.
"""

from __future__ import annotations

from dataclasses import dataclass

from coda.v1 import common_pb2


@dataclass
class WorkerError(Exception):
    """Base for exceptions a handler raises to classify a stage failure.

    code: a short machine-readable identifier (e.g. "GROQ_TIMEOUT",
    "SCHEMA_UNSUPPORTED"). provider_status: the upstream HTTP/gRPC status or
    error code, when there is one, purely for diagnostics.
    """

    message: str
    code: str = "WORKER_ERROR"
    provider_status: str = ""

    def __str__(self) -> str:
        return self.message

    def to_proto(self, *, retryable: bool) -> common_pb2.Error:
        return common_pb2.Error(
            code=self.code,
            message=self.message,
            retryable=retryable,
            provider_status=self.provider_status,
        )


class RetryableError(WorkerError):
    """Transient failure — network, 5xx, timeout, model overload
    (architecture.md §2.5). The orchestrator backs off and re-dispatches."""


class FatalError(WorkerError):
    """Malformed input, unsupported schema, or an unfixable validation
    failure. Routed to stage.dlq immediately — retrying cannot help."""


class QuotaExhaustedError(WorkerError):
    """Daily/minute token or request cap hit. Parks the job without
    consuming an attempt (architecture.md §2.5's "single most important
    operational detail" on a free tier).

    `retry_after_seconds`, when known (e.g. from a provider's Retry-After
    header), lets the orchestrator resume sooner than its one-hour default.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "QUOTA_EXHAUSTED",
        provider_status: str = "",
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message, code=code, provider_status=provider_status)
        self.retry_after_seconds = retry_after_seconds


class CancelledSignal(WorkerError):
    """Cancellation observed mid-stage (architecture.md §4.4). Terminal, no
    retry. Handlers should check the worker's cancellation flag at
    checkpoints and raise this rather than continuing to spend quota."""

    def __init__(
        self, message: str = "cancellation observed mid-stage", *, code: str = "CANCELLED"
    ) -> None:
        super().__init__(message, code=code)


@dataclass(frozen=True, slots=True)
class Classification:
    status: common_pb2.Status
    error: common_pb2.Error
    resume_after_seconds: float | None = None


def classify_exception(exc: BaseException) -> Classification:
    """Maps any exception raised out of a stage handler to a Status + Error.

    A worker-typed exception (RetryableError, FatalError, ...) is trusted
    as-is. Anything else — a bug, an unexpected library exception — defaults
    to RETRYABLE rather than FATAL: most uncaught exceptions in practice are
    transient (a library raising on a flaky connection, an unhandled edge
    case in one input), and RETRYABLE still bounds the damage via the
    stage's max-attempts ceiling (§4.2) before falling through to the DLQ,
    where FATAL would burn the DLQ's "unfixable" classification on bugs that
    a retry — or a redeploy between retries — might actually clear.
    """
    if isinstance(exc, CancelledSignal):
        return Classification(common_pb2.Status.STATUS_CANCELLED, exc.to_proto(retryable=False))
    if isinstance(exc, QuotaExhaustedError):
        return Classification(
            common_pb2.Status.STATUS_QUOTA_EXHAUSTED,
            exc.to_proto(retryable=True),
            resume_after_seconds=exc.retry_after_seconds,
        )
    if isinstance(exc, FatalError):
        return Classification(common_pb2.Status.STATUS_FATAL, exc.to_proto(retryable=False))
    if isinstance(exc, RetryableError):
        return Classification(common_pb2.Status.STATUS_RETRYABLE, exc.to_proto(retryable=True))

    err = common_pb2.Error(
        code="UNCAUGHT_EXCEPTION",
        message=f"{type(exc).__name__}: {exc}",
        retryable=True,
    )
    return Classification(common_pb2.Status.STATUS_RETRYABLE, err)


__all__ = [
    "WorkerError",
    "RetryableError",
    "FatalError",
    "QuotaExhaustedError",
    "CancelledSignal",
    "Classification",
    "classify_exception",
]
