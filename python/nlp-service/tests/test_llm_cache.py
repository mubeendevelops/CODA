import pytest

from nlp_service import db
from nlp_service.llm.cassette import CassetteLLMClient, CassetteTurn
from nlp_service.llm.client import LLMCompletion
from nlp_service.llm_cache import complete_cached


async def test_cache_miss_calls_llm_and_stores(monkeypatch: pytest.MonkeyPatch) -> None:
    stored: dict[str, object] = {}

    async def _no_hit(conn: object, *, model: str, sha256: str) -> None:
        return None

    async def _store(conn: object, *, model: str, sha256: str, completion: LLMCompletion) -> None:
        stored["model"] = model
        stored["sha256"] = sha256
        stored["completion"] = completion

    monkeypatch.setattr(db, "get_cached_completion", _no_hit)
    monkeypatch.setattr(db, "store_completion", _store)

    client = CassetteLLMClient(turns=[CassetteTurn(content="hello", tokens_in=5, tokens_out=2)])
    completion, cache_hit = await complete_cached(
        object(),  # type: ignore[arg-type]
        client,
        model="m1",
        system_prompt="sys",
        user_prompt="usr",
    )

    assert cache_hit is False
    assert completion.content == "hello"
    assert stored["model"] == "m1"
    assert len(client.calls) == 1


async def test_cache_hit_skips_llm_call(monkeypatch: pytest.MonkeyPatch) -> None:
    cached = LLMCompletion(
        content="cached response",
        tokens_in=1,
        tokens_out=1,
        model="m1",
        latency_ms=0,
        cost_estimate=0.0,
    )

    async def _hit(conn: object, *, model: str, sha256: str) -> LLMCompletion:
        return cached

    monkeypatch.setattr(db, "get_cached_completion", _hit)

    client = CassetteLLMClient(turns=[])  # no turns — a real call would raise
    completion, cache_hit = await complete_cached(
        object(),  # type: ignore[arg-type]
        client,
        model="m1",
        system_prompt="sys",
        user_prompt="usr",
    )

    assert cache_hit is True
    assert completion.content == "cached response"
    assert client.calls == []


def test_prompt_sha256_differs_on_user_prompt() -> None:
    a = db.prompt_sha256("sys", "usr-1")
    b = db.prompt_sha256("sys", "usr-2")
    assert a != b
