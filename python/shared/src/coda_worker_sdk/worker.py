"""StageWorker: wires the consumer loop, protojson codec, heartbeats,
metrics, and the exception boundary into the one thing a service needs to
implement — a `StageHandler` that does the actual work and returns an
artifact reference.

This is the "any failure becomes a well-formed error result, never a silent
crash" guarantee the SDK exists to provide: everything between reading an
envelope and publishing a result runs inside `_on_message`'s try/except, and
every exit path — success, a typed WorkerError, or a bare bug — ends in a
StageResult on stage.results, never an uncaught exception that just kills
the asyncio task and leaves the message stuck.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import redis.asyncio as aioredis

from coda.v1 import common_pb2, envelope_pb2
from coda_worker_sdk.codec import SchemaUnsupportedError, decode_envelope, result_fields
from coda_worker_sdk.consumer import ConsumedMessage, StreamConsumer, default_consumer_name
from coda_worker_sdk.errors import classify_exception
from coda_worker_sdk.heartbeat import Heartbeater
from coda_worker_sdk.logging import trace_context
from coda_worker_sdk.metrics import (
    IN_FLIGHT_MESSAGES,
    MESSAGES_RECLAIMED_TOTAL,
    STAGE_DURATION_SECONDS,
    STAGE_OUTCOMES_TOTAL,
    UNCAUGHT_EXCEPTIONS_TOTAL,
)
from coda_worker_sdk.storage import StorageClient
from coda_worker_sdk.streams import (
    FIELD_ATTEMPT,
    FIELD_IDEMPOTENCY_KEY,
    FIELD_JOB_ID,
    FIELD_STAGE,
    FIELD_TRACE_ID,
    STREAM_RESULTS,
    stage_from_name,
    stage_name,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class StageContext:
    """Everything a handler gets for one envelope."""

    envelope: envelope_pb2.StageEnvelope
    storage: StorageClient
    redis: aioredis.Redis
    heartbeat: Heartbeater
    logger: logging.Logger


@dataclass(slots=True)
class StageOutput:
    """What a handler returns on success."""

    result_ref: str
    metrics: common_pb2.StageMetrics = field(default_factory=common_pb2.StageMetrics)


StageHandler = Callable[[StageContext], Awaitable[StageOutput]]
"""Raise a coda_worker_sdk.errors.WorkerError subclass to classify a failure
(RetryableError / FatalError / QuotaExhaustedError / CancelledSignal); any
other exception is treated as an unclassified bug (see `classify_exception`)
rather than crashing the worker."""


class StageWorker:
    """Consumes one request stream, dispatches every envelope on it to
    `handler`, and publishes exactly one StageResult per envelope.

    One StageWorker per request stream — nlp-service runs two (one for
    STAGE_REDACT, one for STAGE_NLP, both delivered on stage.nlp per
    architecture.md §1.2's "nlp-service owns redaction") sharing a
    dispatch-by-stage `handler`, or two StageWorkers pointed at the same
    stream/group with different handlers if the caller prefers to keep them
    separate — either works, since dispatch is by `envelope.stage`, not by
    which StageWorker instance read the message.
    """

    def __init__(
        self,
        *,
        service_name: str,
        redis: aioredis.Redis,
        storage: StorageClient,
        stream: str,
        group: str,
        handler: StageHandler,
        consumer_name: str | None = None,
        concurrency: int = 4,
        batch: int = 16,
        min_idle_ms: int = 5 * 60 * 1000,
        reclaim_every_s: float = 30.0,
        block_ms: int = 5000,
    ) -> None:
        self._service_name = service_name
        self._redis = redis
        self._storage = storage
        self._handler = handler
        self._consumer = StreamConsumer(
            redis=redis,
            stream=stream,
            group=group,
            name=consumer_name or default_consumer_name(service_name),
            block_ms=block_ms,
            batch=batch,
            concurrency=concurrency,
            min_idle_ms=min_idle_ms,
            reclaim_every_s=reclaim_every_s,
        )

    def stop(self) -> None:
        self._consumer.stop()

    async def run(self) -> None:
        await self._consumer.run(self._on_message)

    async def _on_message(self, msg: ConsumedMessage) -> None:
        if msg.reclaimed:
            MESSAGES_RECLAIMED_TOTAL.labels(service=self._service_name, stream=msg.stream).inc()

        try:
            env = decode_envelope(msg.fields)
        except SchemaUnsupportedError as exc:
            # §2.2: reject a version this build does not implement rather
            # than guess. The denormalised fields (job_id, stage, attempt,
            # idempotency_key, trace_id) survive even though the payload
            # itself was never unmarshalled, so a well-formed FATAL result
            # can still be built and echoed back.
            await self._publish_fatal_from_fields(
                msg.fields, code="SCHEMA_UNSUPPORTED", message=str(exc)
            )
            return
        except Exception as exc:
            logger.error(
                "undecodable envelope, publishing best-effort fatal result",
                extra={"extra_fields": {"message_id": msg.id, "error": str(exc)}},
            )
            await self._publish_fatal_from_fields(
                msg.fields, code="MALFORMED_ENVELOPE", message=str(exc)
            )
            return

        with trace_context(env.trace_id):
            IN_FLIGHT_MESSAGES.labels(service=self._service_name).inc()
            started = time.monotonic()
            try:
                await self._handle_envelope(env)
            finally:
                IN_FLIGHT_MESSAGES.labels(service=self._service_name).dec()
                STAGE_DURATION_SECONDS.labels(
                    service=self._service_name, stage=stage_name(env.stage)
                ).observe(time.monotonic() - started)

    async def _handle_envelope(self, env: envelope_pb2.StageEnvelope) -> None:
        heartbeat = Heartbeater(
            self._redis,
            service_name=self._service_name,
            job_id=env.job_id,
            stage=env.stage,
            attempt=env.attempt,
            trace_id=env.trace_id,
        )
        ctx = StageContext(
            envelope=env,
            storage=self._storage,
            redis=self._redis,
            heartbeat=heartbeat,
            logger=logger,
        )

        status: common_pb2.Status
        result_ref = ""
        metrics = common_pb2.StageMetrics()
        error: common_pb2.Error | None = None
        resume_after_seconds: float | None = None

        async with heartbeat:
            try:
                output = await self._handler(ctx)
                status = common_pb2.Status.STATUS_OK
                result_ref = output.result_ref
                metrics = output.metrics
            except Exception as exc:  # the exception boundary
                classification = classify_exception(exc)
                status = classification.status
                error = classification.error
                resume_after_seconds = classification.resume_after_seconds
                if (
                    status == common_pb2.Status.STATUS_RETRYABLE
                    and error.code == "UNCAUGHT_EXCEPTION"
                ):
                    UNCAUGHT_EXCEPTIONS_TOTAL.labels(
                        service=self._service_name, stage=stage_name(env.stage)
                    ).inc()
                logger.error(
                    "stage handler failed",
                    exc_info=True,
                    extra={
                        "extra_fields": {
                            "job_id": env.job_id,
                            "stage": stage_name(env.stage),
                            "status": common_pb2.Status.Name(status),
                        }
                    },
                )

        STAGE_OUTCOMES_TOTAL.labels(
            service=self._service_name,
            stage=stage_name(env.stage),
            status=common_pb2.Status.Name(status),
        ).inc()

        await self._publish_result(
            env,
            status=status,
            result_ref=result_ref,
            metrics=metrics,
            error=error,
            resume_after_seconds=resume_after_seconds,
        )

    async def _publish_result(
        self,
        env: envelope_pb2.StageEnvelope,
        *,
        status: common_pb2.Status,
        result_ref: str,
        metrics: common_pb2.StageMetrics,
        error: common_pb2.Error | None,
        resume_after_seconds: float | None,
    ) -> None:
        res = envelope_pb2.StageResult(
            job_id=env.job_id,
            consultation_id=env.consultation_id,
            stage=env.stage,
            attempt=env.attempt,
            idempotency_key=env.idempotency_key,
            trace_id=env.trace_id,
            schema_version=env.schema_version,
            status=status,
            result_ref=result_ref,
            metrics=metrics,
        )
        if error is not None:
            res.error.CopyFrom(error)
        if resume_after_seconds is not None:
            from google.protobuf.timestamp_pb2 import Timestamp

            ts = Timestamp()
            ts.FromNanoseconds(int((time.time() + resume_after_seconds) * 1e9))
            res.resume_after.CopyFrom(ts)

        # Publishing the result IS the durable-persistence step this worker
        # can offer (it never writes Postgres directly — §1.2). Only after
        # this XADD succeeds does _on_message return normally and let the
        # consumer XACK the original envelope (§2.3's acknowledgement rule);
        # letting this exception propagate is what keeps that ordering
        # correct on a publish failure.
        # See heartbeat.py's identical ignore: dict[str, str] is a legal
        # value for redis-py's wider-unioned, invariant dict param.
        await self._redis.xadd(STREAM_RESULTS, result_fields(res))  # type: ignore[arg-type]

    async def _publish_fatal_from_fields(
        self, fields: dict[str, str], *, code: str, message: str
    ) -> None:
        job_id = fields.get(FIELD_JOB_ID, "")
        if not job_id:
            logger.error(
                "undecodable message carries no job_id, dropping",
                extra={"extra_fields": {"code": code}},
            )
            return
        try:
            stage = stage_from_name(fields.get(FIELD_STAGE, ""))
        except ValueError:
            stage = common_pb2.Stage.STAGE_UNSPECIFIED
        try:
            attempt = int(fields.get(FIELD_ATTEMPT, "0"))
        except ValueError:
            attempt = 0
        res = envelope_pb2.StageResult(
            job_id=job_id,
            stage=stage,
            attempt=attempt,
            idempotency_key=fields.get(FIELD_IDEMPOTENCY_KEY, ""),
            trace_id=fields.get(FIELD_TRACE_ID, ""),
            status=common_pb2.Status.STATUS_FATAL,
            error=common_pb2.Error(code=code, message=message, retryable=False),
        )
        # See heartbeat.py's identical ignore: dict[str, str] is a legal
        # value for redis-py's wider-unioned, invariant dict param.
        await self._redis.xadd(STREAM_RESULTS, result_fields(res))  # type: ignore[arg-type]


__all__ = ["StageContext", "StageOutput", "StageHandler", "StageWorker"]
