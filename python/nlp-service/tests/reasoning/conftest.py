"""Shared fakes for the Module 5/6 tests.

Scoped to `tests/reasoning/` for the same reason `tests/graph/` has its own:
the autouse cache-disabling fixture would otherwise reach `test_llm_cache.py`,
whose whole subject is that cache.

Every test here pins the LEXICAL embedding backend explicitly rather than
letting `resolve_backend` decide. Whether sentence-transformers happens to be
installed in a given environment must never change a test's outcome — a suite
that scores differently on a developer laptop than in CI is not a suite.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import pytest

from coda_worker_sdk.storage import build_artifact_key
from nlp_service import db
from nlp_service.reasoning import embeddings, pipeline


class FakeConn:
    """Stands in for psycopg.AsyncConnection; never used for SQL here."""


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
        # Delegates to the real key builder — found live 2026-09-05 that a
        # naive format string here let `pipeline.py` mint an open-ended,
        # per-field artifact `kind` (`f"trace_{field_key}"`) that every one
        # of this suite's tests happily accepted, while the real
        # `coda_worker_sdk.storage.StorageClient` correctly rejects any kind
        # outside common.proto's closed ArtifactKind set. 165 passing tests
        # never caught it; the first real run against live storage did.
        return build_artifact_key("dev", consultation_id, stage, run_config_id, kind, ext)


class FakeHeartbeat:
    def __init__(self) -> None:
        self.steps: list[str] = []

    def update(self, *, percent: float, step: str) -> None:
        self.steps.append(step)


@pytest.fixture
def fake_conn() -> FakeConn:
    return FakeConn()


@pytest.fixture
def fake_storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def lexical_backend() -> embeddings.LexicalEmbeddingBackend:
    return embeddings.LexicalEmbeddingBackend()


@pytest.fixture(autouse=True)
def _pin_lexical_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """`resolve_backend` would otherwise load MiniLM (slow, and a network
    download on a cold cache) in any environment that has it installed.

    Patched on **every module that imported the name**, not only on
    `embeddings` itself. `pipeline.py` does `from ... import resolve_backend`,
    which binds the function into its own namespace at import time, so
    patching the source module alone left the pipeline loading MiniLM — the
    fixture looked like it was working and was not.
    """
    embeddings.reset_cache()
    lexical = lambda *_a, **_k: embeddings.LexicalEmbeddingBackend()  # noqa: E731
    monkeypatch.setattr(embeddings, "resolve_backend", lexical)
    monkeypatch.setattr(pipeline, "resolve_backend", lexical)


@pytest.fixture(autouse=True)
def _no_real_llm_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_hit(conn: object, *, model: str, sha256: str) -> None:
        return None

    async def _no_store(conn: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(db, "get_cached_completion", _no_hit)
    monkeypatch.setattr(db, "store_completion", _no_store)
