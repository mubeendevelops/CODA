"""Async Postgres connection pool for a worker with real DB access, mirroring
storage.py's shape (a thin wrapper the service constructs once at startup
and passes into handlers via closure). Uses psycopg3's native asyncio
support, same driver family as coda_eval's sync psycopg client (different
concurrency model: coda_eval is a one-shot host CLI, this is a long-running
in-process consumer loop that needs a real pool, not one connection).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
from psycopg_pool import AsyncConnectionPool

from coda_worker_sdk.config import PostgresConfig


class PostgresPool:
    def __init__(self, cfg: PostgresConfig, *, min_size: int = 1, max_size: int = 10) -> None:
        self._pool = AsyncConnectionPool(
            conninfo=cfg.dsn(), min_size=min_size, max_size=max_size, open=False
        )

    async def open(self) -> None:
        await self._pool.open(wait=True)

    async def close(self) -> None:
        await self._pool.close()

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[psycopg.AsyncConnection]:
        """`async with pool.connection() as conn:` — one pooled connection,
        checked back in on exit (psycopg_pool's own behavior; this thin
        wrapper exists purely to give call sites a precisely-typed async
        context manager rather than psycopg_pool's generic one).
        """
        async with self._pool.connection() as conn:
            yield conn


__all__ = ["PostgresPool"]
