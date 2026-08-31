import json

import pytest

from coda_worker_sdk.errors import FatalError
from nlp_service import db
from nlp_service.extraction import run_extraction
from nlp_service.llm.cassette import CassetteLLMClient, CassetteTurn

VALID = {
    "chief_complaint": {"value": "cough", "source_turn_ids": [0], "confidence": 0.9},
    "hopi": None,
    "past_medical_history": [],
    "medications": [],
    "allergies": [],
    "examination_findings": None,
    "provisional_diagnosis": [],
    "investigations_advised": [],
    "treatment_plan": None,
}


@pytest.fixture(autouse=True)
def _no_real_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """extraction.py routes every LLM call through llm_cache.complete_cached,
    which touches Postgres via nlp_service.db. These tests exercise the
    repair-loop logic, not the cache — bypass the DB entirely (always a
    miss, store is a no-op) so no real connection is needed.
    """

    async def _no_hit(conn: object, *, model: str, sha256: str) -> None:
        return None

    async def _no_store(conn: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(db, "get_cached_completion", _no_hit)
    monkeypatch.setattr(db, "store_completion", _no_store)


async def test_first_attempt_valid_needs_no_repair() -> None:
    client = CassetteLLMClient(
        turns=[CassetteTurn(content=json.dumps(VALID), tokens_in=100, tokens_out=50)]
    )
    result = await run_extraction(
        conn=object(),  # type: ignore[arg-type]
        llm_client=client,
        model="qwen/qwen3.8-27b",
        transcript_turns_text="0: doctor: what brings you in today?\n0: patient: I have a cough",
        known_turn_ids={0},
        consultation_id="c1",
        run_config_id="rc1",
        repair_max_attempts=2,
        timeout_s=5.0,
    )
    assert result.schema_valid is True
    assert result.repair_attempts == 0
    assert result.llm_calls == 1
    assert result.tokens_in == 100
    assert result.tokens_out == 50
    assert result.note.chief_complaint.value == "cough"
    assert list(result.note.chief_complaint.source_turn_ids) == [0]


async def test_invalid_then_valid_repair_succeeds() -> None:
    client = CassetteLLMClient(
        turns=[
            CassetteTurn(content="not json", tokens_in=10, tokens_out=5),
            CassetteTurn(content=json.dumps(VALID), tokens_in=20, tokens_out=15),
        ]
    )
    result = await run_extraction(
        conn=object(),  # type: ignore[arg-type]
        llm_client=client,
        model="qwen/qwen3.8-27b",
        transcript_turns_text="0: doctor: hello",
        known_turn_ids={0},
        consultation_id="c1",
        run_config_id="rc1",
        repair_max_attempts=2,
        timeout_s=5.0,
    )
    assert result.schema_valid is True
    assert result.repair_attempts == 1
    assert result.llm_calls == 2
    assert result.tokens_in == 30
    assert result.tokens_out == 20

    # The repair call must actually carry the validation error and the
    # previous bad response, not a bare retry of the original prompt.
    second_call_prompt = str(client.calls[1]["user_prompt"])
    assert "not valid JSON" in second_call_prompt
    assert "not json" in second_call_prompt


async def test_repair_budget_exhausted_raises_fatal() -> None:
    client = CassetteLLMClient(
        turns=[
            CassetteTurn(content="not json", tokens_in=1, tokens_out=1),
            CassetteTurn(content="still not json", tokens_in=1, tokens_out=1),
            CassetteTurn(content="still still not json", tokens_in=1, tokens_out=1),
        ]
    )
    with pytest.raises(FatalError) as exc_info:
        await run_extraction(
            conn=object(),  # type: ignore[arg-type]
            llm_client=client,
            model="qwen/qwen3.8-27b",
            transcript_turns_text="0: doctor: hello",
            known_turn_ids={0},
            consultation_id="c1",
            run_config_id="rc1",
            repair_max_attempts=2,
            timeout_s=5.0,
        )
    assert exc_info.value.code == "EXTRACTION_SCHEMA_INVALID"
    assert not client.turns  # all 3 scripted turns were consumed (1 + 2 repairs)


async def test_unknown_turn_id_triggers_repair() -> None:
    hallucinated = dict(VALID)
    hallucinated["chief_complaint"] = {
        "value": "cough",
        "source_turn_ids": [99],
        "confidence": 0.9,
    }
    client = CassetteLLMClient(
        turns=[
            CassetteTurn(content=json.dumps(hallucinated), tokens_in=1, tokens_out=1),
            CassetteTurn(content=json.dumps(VALID), tokens_in=1, tokens_out=1),
        ]
    )
    result = await run_extraction(
        conn=object(),  # type: ignore[arg-type]
        llm_client=client,
        model="qwen/qwen3.8-27b",
        transcript_turns_text="0: doctor: hello",
        known_turn_ids={0},
        consultation_id="c1",
        run_config_id="rc1",
        repair_max_attempts=1,
        timeout_s=5.0,
    )
    assert result.schema_valid is True
    assert result.repair_attempts == 1
