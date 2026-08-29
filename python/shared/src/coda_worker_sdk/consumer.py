"""Redis Streams consumer-group loop: XREADGROUP + XAUTOCLAIM recovery,
concurrency limits, and prefetch control, with graceful shutdown.

Mirrors go/internal/queue/consumer.go's Consumer — same two-source loop
(fresh reads interleaved with a periodic reclaim sweep), same acknowledgement
rule (§2.3: XACK only after the handler reports durable persistence), same
recovery story (§2.4: a dead consumer's PEL entries get reclaimed by
XAUTOCLAIM past the stream's visibility timeout). Concurrency is the one
axis the Go side didn't need an equivalent of — go-orchestrator processes a
message per goroutine implicitly; here `concurrency` bounds how many
envelopes one worker process handles in parallel, and `batch` (the XREADGROUP
COUNT) bounds how many are prefetched ahead of that.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import redis.asyncio as aioredis
from redis.exceptions import ResponseError

from coda_worker_sdk.streams import group_for_stream

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ConsumedMessage:
    """One consumed stream entry, decoupled from redis-py's tuple shape so
    handlers never import redis.asyncio directly. Mirrors
    go/internal/queue/consumer.go's Message.
    """

    id: str
    stream: str
    group: str
    fields: dict[str, str]
    reclaimed: bool = False
    delivery_count: int = 0

    def field(self, name: str) -> str:
        return self.fields.get(name, "")


Handler = Callable[[ConsumedMessage], Awaitable[None]]
"""Processes one message. Returning normally means "durably persisted — safe
to acknowledge"; the consumer XACKs only then (§2.3). Raising leaves the
entry in the PEL so a later reclaim sweep retries it — the same path a crash
would have taken. A handler must therefore never return normally on a path
that skipped persistence: that is the one mistake this design cannot detect
(go/internal/queue/consumer.go documents the same warning, verbatim)."""


@dataclass(slots=True)
class StreamConsumer:
    redis: aioredis.Redis
    stream: str
    group: str
    name: str
    """Must be stable across restarts of the same process consumer identity
    and distinct across replicas — the same constraint as the Go side."""
    block_ms: int = 5000
    batch: int = 16
    """XREADGROUP COUNT — the prefetch bound."""
    concurrency: int = 4
    """Max envelopes handled concurrently by this process."""
    min_idle_ms: int = 5 * 60 * 1000
    """XAUTOCLAIM's min-idle threshold — the stage's visibility timeout."""
    reclaim_every_s: float = 30.0

    _stop: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _semaphore: asyncio.Semaphore = field(init=False, repr=False)
    _in_flight: set[asyncio.Task[None]] = field(default_factory=set, init=False, repr=False)

    def __post_init__(self) -> None:
        self._semaphore = asyncio.Semaphore(self.concurrency)

    def stop(self) -> None:
        """Signals the run loop to stop accepting new work. Call `wait_drained`
        (or just `await run(...)` returning) to wait for in-flight handlers.
        """
        self._stop.set()

    async def ensure_group(self) -> None:
        """Creates the consumer group at the stream's tail ("$"), creating
        the stream if needed. Idempotent — BUSYGROUP is swallowed, matching
        go/internal/queue/client.go's EnsureGroup.
        """
        try:
            await self.redis.xgroup_create(self.stream, self.group, id="$", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def run(self, handler: Handler) -> None:
        """Consumes until `stop()` is called, then waits for in-flight
        handlers to finish before returning. Interleaves fresh reads with
        periodic reclaim sweeps so a consumer that restarts after a crash
        picks its own abandoned entries back up without waiting a full
        reclaim interval.
        """
        await self.ensure_group()

        n = await self._reclaim_once(handler)
        if n:
            logger.info(
                "reclaimed pending entries at startup",
                extra={"extra_fields": {"count": n, "stream": self.stream}},
            )

        loop = asyncio.get_running_loop()
        last_reclaim = loop.time()
        try:
            while not self._stop.is_set():
                now = loop.time()
                if now - last_reclaim >= self.reclaim_every_s:
                    last_reclaim = now
                    try:
                        await self._reclaim_once(handler)
                    except Exception:
                        logger.exception(
                            "reclaim sweep failed", extra={"extra_fields": {"stream": self.stream}}
                        )

                try:
                    messages = await self._read()
                except Exception:
                    logger.exception(
                        "xreadgroup failed", extra={"extra_fields": {"stream": self.stream}}
                    )
                    await asyncio.sleep(1.0)
                    continue

                for msg in messages:
                    await self._dispatch(handler, msg)
        finally:
            if self._in_flight:
                await asyncio.gather(*self._in_flight, return_exceptions=True)

    async def _read(self) -> list[ConsumedMessage]:
        resp = await self.redis.xreadgroup(
            groupname=self.group,
            consumername=self.name,
            streams={self.stream: ">"},
            count=self.batch,
            block=self.block_ms,
        )
        if not resp:
            return []
        out: list[ConsumedMessage] = []
        for stream_name, entries in resp:
            for entry_id, fields in entries:
                out.append(
                    ConsumedMessage(
                        id=entry_id, stream=stream_name, group=self.group, fields=dict(fields)
                    )
                )
        return out

    async def _reclaim_once(self, handler: Handler) -> int:
        total = 0
        start = "0-0"
        while True:
            next_cursor, claimed, _deleted = await self.redis.xautoclaim(
                name=self.stream,
                groupname=self.group,
                consumername=self.name,
                min_idle_time=self.min_idle_ms,
                start_id=start,
                count=self.batch,
            )
            for entry_id, fields in claimed:
                msg = ConsumedMessage(
                    id=entry_id,
                    stream=self.stream,
                    group=self.group,
                    fields=dict(fields),
                    reclaimed=True,
                )
                await self._dispatch(handler, msg)
                total += 1
            if next_cursor in ("0-0", "0", "") or not claimed:
                return total
            start = next_cursor

    async def _dispatch(self, handler: Handler, msg: ConsumedMessage) -> None:
        """Bounds concurrency: acquires the semaphore before spawning a task,
        so `run` never reads further ahead than `batch` while `concurrency`
        handlers are already busy — the prefetch/concurrency split the
        module docstring describes.
        """
        await self._semaphore.acquire()
        task = asyncio.ensure_future(self._handle(handler, msg))
        self._in_flight.add(task)
        task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        self._in_flight.discard(task)
        self._semaphore.release()

    async def _handle(self, handler: Handler, msg: ConsumedMessage) -> None:
        try:
            await handler(msg)
        except Exception:
            # Deliberately not acknowledged: the entry stays in the PEL and
            # a later reclaim sweep retries it. This is the correct response
            # to "the handler could not persist its result" — acking here
            # would drop it (go/internal/queue/consumer.go's `handle`, same
            # rationale).
            logger.exception(
                "handler failed, leaving entry pending for reclaim",
                extra={
                    "extra_fields": {
                        "stream": msg.stream,
                        "message_id": msg.id,
                        "job_id": msg.field("job_id"),
                    }
                },
            )
            return
        try:
            await self.redis.xack(msg.stream, msg.group, msg.id)
        except Exception:
            # The work is durably persisted; only the ack failed. The entry
            # will be reclaimed and re-handled, absorbed by convergent
            # result application on the orchestrator side (§2.3).
            logger.warning(
                "ack failed after successful handling",
                extra={"extra_fields": {"stream": msg.stream, "message_id": msg.id}},
            )


def default_consumer_name(service_name: str) -> str:
    """A stable-enough default: hostname is the container ID under Compose,
    so restarts of the *same* container reuse it (recognising its own PEL
    entries) while distinct replicas/containers get distinct names.
    """
    import socket

    return f"{service_name}-{socket.gethostname()}"


__all__ = [
    "ConsumedMessage",
    "Handler",
    "StreamConsumer",
    "default_consumer_name",
    "group_for_stream",
]
