"""Progress heartbeat publishing (architecture.md §2.4).

A worker starts a Heartbeater when it begins a stage and stops it when the
stage finishes (success or failure) — the orchestrator's reaper treats a
worker whose heartbeats stopped more than 90s ago as stalled even inside the
visibility window, so the cadence here (15s, `HEARTBEAT_INTERVAL_SECONDS`)
matters operationally, not just cosmetically.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import redis.asyncio as aioredis

from coda.v1 import common_pb2, envelope_pb2
from coda_worker_sdk.codec import heartbeat_fields
from coda_worker_sdk.metrics import HEARTBEATS_PUBLISHED_TOTAL
from coda_worker_sdk.streams import STREAM_PROGRESS, stage_name

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 15.0
"""Mirrors go/internal/queue/policy.go's HeartbeatInterval."""


class Heartbeater:
    """Publishes a StageHeartbeat every HEARTBEAT_INTERVAL_SECONDS while
    active, carrying the caller's current percent/step. Runs as a background
    asyncio task; `percent`/`step` are read fresh on every tick via the
    mutable `state` this class owns, so a handler updates progress by
    calling `update()` rather than restarting the heartbeat loop.
    """

    def __init__(
        self,
        redis: aioredis.Redis,
        *,
        service_name: str,
        job_id: str,
        stage: common_pb2.Stage,
        attempt: int,
        trace_id: str,
        interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        self._redis = redis
        self._service_name = service_name
        self._job_id = job_id
        self._stage = stage
        self._attempt = attempt
        self._trace_id = trace_id
        self._interval = interval_seconds
        self._percent = 0.0
        self._step = ""
        self._task: asyncio.Task[None] | None = None

    def update(self, *, percent: float | None = None, step: str | None = None) -> None:
        if percent is not None:
            self._percent = max(0.0, min(100.0, percent))
        if step is not None:
            self._step = step

    async def _publish_once(self) -> None:
        from google.protobuf.timestamp_pb2 import Timestamp

        now = Timestamp()
        now.GetCurrentTime()
        hb = envelope_pb2.StageHeartbeat(
            job_id=self._job_id,
            stage=self._stage,
            attempt=self._attempt,
            percent_complete=self._percent,
            step=self._step,
            trace_id=self._trace_id,
            at=now,
        )
        try:
            # redis-py's stub wants a dict keyed/valued by a wider union that
            # includes str; dict's invariance rejects our dict[str, str]
            # even though every value in it is a legal member of that union.
            await self._redis.xadd(STREAM_PROGRESS, heartbeat_fields(hb))  # type: ignore[arg-type]
            HEARTBEATS_PUBLISHED_TOTAL.labels(
                service=self._service_name, stage=stage_name(self._stage)
            ).inc()
        except Exception:
            # Best-effort by design (architecture.md §2.4's heartbeat is a
            # convenience for stall detection, not correctness) — a dropped
            # heartbeat costs a stale progress percentage, never pipeline
            # correctness, so it must never fail the stage it's reporting on.
            logger.warning(
                "heartbeat publish failed", extra={"extra_fields": {"job_id": self._job_id}}
            )

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self._publish_once()

    async def __aenter__(self) -> Heartbeater:
        await self._publish_once()  # first heartbeat lands immediately
        self._task = asyncio.ensure_future(self._loop())
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None


__all__ = ["Heartbeater", "HEARTBEAT_INTERVAL_SECONDS"]
