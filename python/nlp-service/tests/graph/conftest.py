"""Shared fakes for the graph tests.

Scoped to `tests/graph/` rather than the whole test directory: the autouse
cache-disabling fixture below would otherwise reach `test_llm_cache.py`,
whose entire subject is that cache.

`FakeConn` stands in for a `psycopg.AsyncConnection` in tests that exercise
construction/assembly logic but not SQL: `llm_cache.complete_cached` takes a
connection and the graph code passes it straight through, so a test that
never touches Postgres still needs an object to hand along. Tests that
actually exercise SQL (store.py) are marked and skipped without a database
rather than mocking a cursor into agreeing with itself.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import pytest

from nlp_service import db


class FakeConn:
    """Never used for SQL in these tests — the cache is monkeypatched off."""


@dataclass
class FakePool:
    conn: FakeConn = field(default_factory=FakeConn)

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[FakeConn]:
        yield self.conn


@dataclass
class FakeStorage:
    objects: dict[str, bytes] = field(default_factory=dict)

    async def get_bytes(self, key: str) -> bytes:
        return self.objects[key]

    async def put_bytes(self, key: str, data: bytes, *, content_type: str = "") -> str:
        self.objects[key] = data
        return "sha256-fake"

    def artifact_key(
        self, consultation_id: str, stage: str, run_config_id: str, kind: str, ext: str
    ) -> str:
        return f"dev/consultations/{consultation_id}/stages/{stage}/{run_config_id}/{kind}.{ext}"


@pytest.fixture
def fake_conn() -> FakeConn:
    return FakeConn()


@pytest.fixture
def fake_storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture(autouse=True)
def _no_real_llm_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every graph test goes through `complete_cached`, which reads and
    writes Postgres. Disabling the cache (rather than faking a hit) keeps the
    cassette's call sequence exactly the sequence the code issued — a cache
    hit would swallow a call and make an assertion about call count silently
    wrong.
    """

    async def _no_hit(conn: object, *, model: str, sha256: str) -> None:
        return None

    async def _no_store(conn: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(db, "get_cached_completion", _no_hit)
    monkeypatch.setattr(db, "store_completion", _no_store)
