"""Modules 5 and 6 end to end, and the claim the ablation depends on:
**every stage switch is configuration, not a code branch**.

If a knob in `RunConfig` did not actually change behaviour, the corresponding
arm would silently be a duplicate of another and the ablation table would
report a difference that does not exist. So each knob is asserted to change
something observable, and `got_k2_nograph` in particular is asserted to
differ from `got_k2` ONLY in which thoughts reach the prompt.

The second theme is cost accounting. Generation is one batched call covering
every field, so its per-field cost is an apportionment and is labelled as one
— a test pins that the apportionment is actually applied rather than the
whole batch being charged to each field.
"""

from __future__ import annotations

import json

import pytest
from factories import Cat, EdgeT, Pol, edge, run_config, thought

from coda.v1 import thought_pb2
from coda_worker_sdk.errors import FatalError
from nlp_service.llm.cassette import CassetteLLMClient, CassetteTurn
from nlp_service.reasoning import distill, pipeline, store
from nlp_service.reasoning.generation import FIELDS_PER_GENERATION_CALL, LIST_FIELDS, chunk_fields
from nlp_service.reasoning.pipeline import resolve_stage_config, run_reasoning

FIELDS = distill.FIELD_KEYS
SUMMARY = "The patient reported two days of chest pain, worse on exertion, with no fever."
N_CHUNKS = len(chunk_fields(list(FIELDS), FIELDS_PER_GENERATION_CALL))
"""How many generation calls ONE variant costs now that a variant's fields
are split across several calls (see generation.py's max_tokens history note)
— every call-count assertion below is in terms of this, not 1."""


@pytest.fixture(autouse=True)
def _no_sql_writes(monkeypatch: pytest.MonkeyPatch) -> dict:
    """`store.write_reasoning_outputs` has its own live-Postgres test; here it
    is recorded rather than executed so these tests stay network- and
    database-free."""
    written: dict = {}

    async def _write(conn, **kwargs):  # type: ignore[no-untyped-def]
        written.update(kwargs)

    monkeypatch.setattr(store, "write_reasoning_outputs", _write)
    monkeypatch.setattr(pipeline.store, "write_reasoning_outputs", _write)
    return written


@pytest.fixture
def written(_no_sql_writes: dict) -> dict:
    return _no_sql_writes


def graph() -> thought_pb2.ThoughtGraph:
    """A small consultation touching several fields, with one negation so the
    polarity path is live."""
    thoughts = [
        thought("t1", 1, "chest pain for two days", ["chest pain"]),
        thought("t2", 3, "worse on exertion", ["exertion"]),
        thought("t3", 9, "no fever at any point", ["fever"], polarity=Pol.POLARITY_NEGATED),
        thought(
            "t4", 2, "diabetes for ten years", ["diabetes"], category=Cat.THOUGHT_CATEGORY_HISTORY
        ),
        thought(
            "t5", 5, "penicillin allergy", ["penicillin"], category=Cat.THOUGHT_CATEGORY_ALLERGY
        ),
        thought(
            "t6", 11, "start aspirin daily", ["aspirin"], category=Cat.THOUGHT_CATEGORY_PLAN
        ),
    ]
    edges = [
        edge("t1", "t2", EdgeT.EDGE_TYPE_ELABORATION, 0.9),
        edge("t1", "t3", EdgeT.EDGE_TYPE_NEGATION, 1.0),
        edge("t1", "t6", EdgeT.EDGE_TYPE_CAUSAL, 0.7),
    ]
    return thought_pb2.ThoughtGraph(
        consultation_id="c1", run_config_id="rc1", thoughts=thoughts, edges=edges
    )


def generation_response(field_keys, *, cc="chest pain for two days", ids=None) -> str:
    """A valid response for exactly `field_keys` — one generation call's
    worth, now that a call covers a chunk of fields rather than all nine."""
    ids = ids or ["t1"]
    fields = {}
    for fk in field_keys:
        if fk == "chief_complaint":
            fields[fk] = {
                "value": cc, "items": [], "source_thought_ids": ids, "confidence": 0.9
            }
        elif fk == "hopi":
            fields[fk] = {
                "value": "worse on exertion, no fever",
                "items": [],
                "source_thought_ids": ["t2", "t3"],
                "confidence": 0.8,
            }
        elif fk == "past_medical_history":
            fields[fk] = {
                "value": None, "items": ["diabetes"], "source_thought_ids": ["t4"],
                "confidence": 0.9,
            }
        elif fk == "allergies":
            fields[fk] = {
                "value": None, "items": ["penicillin"], "source_thought_ids": ["t5"],
                "confidence": 0.9,
            }
        elif fk == "treatment_plan":
            fields[fk] = {
                "value": "start aspirin daily", "items": [], "source_thought_ids": ["t6"],
                "confidence": 0.9,
            }
        elif fk in ("medications", "provisional_diagnosis", "investigations_advised"):
            fields[fk] = {"value": None, "items": [], "source_thought_ids": [], "confidence": 0.0}
        else:
            fields[fk] = {"value": None, "items": [], "source_thought_ids": [], "confidence": 0.0}
    return json.dumps({"fields": fields})


def judge_response(n: int) -> str:
    """Deliberately inverts the heuristic's usual ordering so a test can tell
    which backend actually drove selection."""
    return json.dumps(
        {
            "scores": [
                {
                    "index": i,
                    "relevance": 0.1 if i == 0 else 0.95,
                    "consistency": 0.1 if i == 0 else 0.95,
                    "redundancy": 0.0,
                    "rationale": f"candidate {i}",
                }
                for i in range(n)
            ]
        }
    )


REFINE_EVIDENCE: dict[str, list[str]] = {
    "chief_complaint": ["t1"],
    "hopi": ["t1"],
    "past_medical_history": ["t4"],
    "medications": [],
    "allergies": ["t5"],
    "examination_findings": [],
    "provisional_diagnosis": [],
    "investigations_advised": [],
    "treatment_plan": ["t6"],
}
"""A thought id that is genuinely in each field's retrieved subgraph.

A revision may only cite evidence the field was actually shown — the same
provenance rule generation obeys — so a one-size-fits-all critique fixture
would be rejected for every field whose subgraph does not contain the cited
id, and the test would be measuring the validator rather than the loop. The
empty lists are the fields this fixture's graph supports no thoughts for; a
revision there must be empty and cite nothing.
"""


def critique_response(field_key: str) -> str:
    """A valid revision for one specific field."""
    ids = REFINE_EVIDENCE[field_key]
    is_list = field_key in LIST_FIELDS
    if not ids:
        revised = {"value": None, "items": [], "source_thought_ids": [], "confidence": 0.0}
    elif is_list:
        revised = {
            "value": None,
            "items": ["refined item"],
            "source_thought_ids": ids,
            "confidence": 0.95,
        }
    else:
        revised = {
            "value": "refined text covering the evidence",
            "items": [],
            "source_thought_ids": ids,
            "confidence": 0.95,
        }
    return json.dumps({"critique": "could cover more of the evidence", "revised": revised})


N_FIELDS = len(FIELDS)
"""Generation fills a candidate for EVERY field — an empty one where the
graph supports nothing — so scoring and refinement run over all nine, not
only the five the fixture populates with text."""


def turns_for(
    n: int, k: int, *, judge: bool = False, gen_tokens: tuple[int, int] = (0, 0)
) -> list[CassetteTurn]:
    """The exact call budget a run of (N, K) spends, in order: N variants x
    N_CHUNKS generation calls (one call per `FIELDS_PER_GENERATION_CALL`-
    sized slice of fields, not one call per variant), then per field a judge
    call (if the judge is scoring) and K refinement calls, then one summary
    call. `gen_tokens` is charged to EVERY chunk call, so the true total
    across one variant is `gen_tokens * N_CHUNKS`."""
    turns = [
        CassetteTurn(
            content=generation_response(chunk), tokens_in=gen_tokens[0], tokens_out=gen_tokens[1]
        )
        for _ in range(n)
        for chunk in chunk_fields(list(FIELDS), FIELDS_PER_GENERATION_CALL)
    ]
    for field_key in FIELDS:
        if judge:
            turns.append(CassetteTurn(content=judge_response(n)))
        turns += [CassetteTurn(content=critique_response(field_key)) for _ in range(k)]
    turns.append(CassetteTurn(content=SUMMARY))
    return turns


async def run(
    fake_conn, fake_storage, *, rc, turns: list[CassetteTurn]
):  # type: ignore[no-untyped-def]
    client = CassetteLLMClient(turns=turns)
    result = await run_reasoning(
        conn=fake_conn,
        storage=fake_storage,
        llm_client=client,
        graph=graph(),
        run_config=rc,
        consultation_id="c1",
        run_config_id="rc1",
        repair_max_attempts=2,
        timeout_s=5.0,
    )
    return result, client


# ------------------------------------------------------------ config defaults


def test_recorded_defaults_apply_to_unset_proto_scalars() -> None:
    """N=3 and K=2 are the recorded defaults (claude_context.md §6). proto3
    cannot tell 'unset' from 0, so this is the one place the defaults live."""
    n, k, backend, ctx = resolve_stage_config(run_config(n_candidates=0, k_iterations=0))
    assert (n, k) == (3, 2)
    assert backend == "heuristic"
    assert ctx == 300


def test_explicit_config_overrides_the_defaults() -> None:
    n, k, backend, ctx = resolve_stage_config(
        run_config(
            n_candidates=1, k_iterations=1, scorer_backend="both", generation_context_tokens=150
        )
    )
    assert (n, k, backend, ctx) == (1, 1, "both", 150)


# ------------------------------------------------------------------- end to end


async def test_a_full_run_produces_a_note_a_summary_and_artifacts(
    fake_conn, fake_storage, written
) -> None:
    rc = run_config(n_candidates=1, k_iterations=1)
    result, _ = await run(fake_conn, fake_storage, rc=rc, turns=turns_for(1, 1))

    assert result.note.chief_complaint.value
    assert list(result.note.chief_complaint.source_turn_ids) == [1]
    assert [v.value for v in result.note.past_medical_history] == ["diabetes"]
    assert result.summary_text == SUMMARY

    # One combined "candidate_set" artifact for every field's trace (kind is
    # a closed ArtifactKind, so per-field traces share one artifact rather
    # than minting a kind per field — see pipeline.py's persist step), plus
    # the note and the summary.
    candidate_set_artifacts = [k for k in fake_storage.objects if k.endswith("candidate_set.json")]
    assert len(candidate_set_artifacts) == 1
    combined_traces = json.loads(fake_storage.objects[candidate_set_artifacts[0]])
    assert len(combined_traces) == len(result.outcomes) == N_FIELDS
    assert result.note_artifact_key.endswith("clinical_note.json")
    assert result.summary_artifact_key.endswith("summary.json")
    assert fake_storage.objects[result.note_artifact_key]
    assert written["summary_text"] == SUMMARY


async def test_the_persisted_trace_carries_the_provenance_distillation_resolved(
    fake_conn, fake_storage
) -> None:
    """An ordering dependency worth pinning: only Module 6 can turn cited
    thought ids into turn ids, and the trace's `final_value` is written after
    it. If the trace were written first, every persisted trace would claim no
    provenance while the note itself looked correct."""
    result, _ = await run(
        fake_conn, fake_storage, rc=run_config(n_candidates=1, k_iterations=1),
        turns=turns_for(1, 1),
    )
    key = [k for k in fake_storage.objects if k.endswith("candidate_set.json")][0]
    combined_traces = json.loads(fake_storage.objects[key])
    final_value = combined_traces["chief_complaint"]["final_value"]
    assert final_value["value"]
    assert final_value["source_turn_ids"] == [1]
    assert final_value["confidence"] > 0


async def test_an_empty_graph_fails_loudly(fake_conn, fake_storage) -> None:
    """Reasoning over nothing would emit an empty note under a GoT run_config
    — an unlabeled result in the ablation table."""
    client = CassetteLLMClient(turns=[])
    with pytest.raises(FatalError) as exc:
        await run_reasoning(
            conn=fake_conn,
            storage=fake_storage,
            llm_client=client,
            graph=thought_pb2.ThoughtGraph(consultation_id="c1", run_config_id="rc1"),
            run_config=run_config(),
            consultation_id="c1",
            run_config_id="rc1",
            repair_max_attempts=2,
            timeout_s=5.0,
        )
    assert exc.value.code == "EMPTY_GRAPH"


# ----------------------------------------------------- the knobs, one by one


async def test_n_candidates_changes_the_number_of_generation_calls(
    fake_conn, fake_storage
) -> None:
    """N is the knob separating a single-candidate run from candidate
    competition; if it did not change call count it would not be doing
    anything."""
    _, one = await run(
        fake_conn, fake_storage,
        rc=run_config(n_candidates=1, k_iterations=1), turns=turns_for(1, 1),
    )
    _, three = await run(
        fake_conn, fake_storage,
        rc=run_config(n_candidates=3, k_iterations=1), turns=turns_for(3, 1),
    )
    assert len(one.calls) == N_CHUNKS + N_FIELDS + 1
    assert len(three.calls) == 3 * N_CHUNKS + N_FIELDS + 1
    assert len(three.calls) - len(one.calls) == 2 * N_CHUNKS, (
        "two extra variants, two extra variants' worth of chunk calls"
    )


async def test_k_scales_refinement_calls_and_the_trajectory(fake_conn, fake_storage) -> None:
    """K is the knob the `got_k1` vs `got_k2` arm comparison turns on. One
    trajectory point per iteration, plus iteration 0's generation score."""
    k1, c1 = await run(
        fake_conn, fake_storage,
        rc=run_config(n_candidates=1, k_iterations=1), turns=turns_for(1, 1),
    )
    k2, c2 = await run(
        fake_conn, fake_storage,
        rc=run_config(n_candidates=1, k_iterations=2), turns=turns_for(1, 2),
    )
    assert len(c1.calls) == N_CHUNKS + N_FIELDS + 1
    assert len(c2.calls) == N_CHUNKS + 2 * N_FIELDS + 1
    assert all(len(list(o.trace.score_trajectory)) == 2 for o in k1.outcomes.values())
    assert all(len(list(o.trace.score_trajectory)) == 3 for o in k2.outcomes.values())


async def test_k_of_zero_is_read_as_unset_and_takes_the_default(fake_conn, fake_storage) -> None:
    """A documented proto3 limitation, pinned so it stays deliberate.

    `k_iterations = 0` is indistinguishable from "field never set" in proto3,
    and `resolve_stage_config` resolves the ambiguity toward the recorded
    default of 2. The consequence is that **K=0 is not reachable through a run
    config** — an arm wanting generation-only behaviour has to be built a
    different way. The ablation matrix only needs K=1 and K=2, so this costs
    nothing today; it is pinned because a future K=0 arm would otherwise
    silently run K=2 and report the difference as a null result.
    """
    n, k, _, _ = resolve_stage_config(run_config(n_candidates=1, k_iterations=0))
    assert k == 2
    result, client = await run(
        fake_conn, fake_storage,
        rc=run_config(n_candidates=1, k_iterations=0), turns=turns_for(1, 2),
    )
    assert len(client.calls) == N_CHUNKS + 2 * N_FIELDS + 1
    assert all(len(list(o.trace.score_trajectory)) == 3 for o in result.outcomes.values())


async def test_graph_context_off_shows_every_field_every_thought(
    fake_conn, fake_storage
) -> None:
    """`got_k2_nograph` is the retrieval control. It must differ from the
    graph arm only in the prompt's evidence blocks — same generation, same
    scoring, same refinement, same distillation."""
    _, with_graph = await run(
        fake_conn, fake_storage,
        rc=run_config(n_candidates=1, k_iterations=1, graph_context_enabled=True),
        turns=turns_for(1, 1),
    )
    _, without = await run(
        fake_conn, fake_storage,
        rc=run_config(n_candidates=1, k_iterations=1, graph_context_enabled=False),
        turns=turns_for(1, 1),
    )
    graph_prompt = with_graph.calls[0]["user_prompt"]
    flat_prompt = without.calls[0]["user_prompt"]
    assert graph_prompt != flat_prompt
    # Every thought appears under every field when retrieval is off, so the
    # allergy thought turns up in the chief-complaint block too.
    assert flat_prompt.count("t5") > graph_prompt.count("t5")
    # Same system prompt, and the same number of calls: the difference is
    # evidence, not instructions and not extra work.
    assert with_graph.calls[0]["system_prompt"] == without.calls[0]["system_prompt"]
    assert len(with_graph.calls) == len(without.calls)


async def test_scorer_backend_both_records_two_scores_and_the_judge_decides(
    fake_conn, fake_storage
) -> None:
    """`both` is what lets the report ask whether the free scorer and the paid
    one agree — which needs the non-selecting backend's score kept, not
    discarded."""
    result, client = await run(
        fake_conn, fake_storage,
        rc=run_config(n_candidates=2, k_iterations=1, scorer_backend="both"),
        turns=turns_for(2, 1, judge=True),
    )
    assert len(client.calls) == 2 * N_CHUNKS + 2 * N_FIELDS + 1
    for outcome in result.outcomes.values():
        assert outcome.score.scorer_backend == "llm_judge", "the judge drives selection"
        assert outcome.secondary_score is not None
        assert outcome.secondary_score.scorer_backend == "heuristic"
        # The judge scored candidate 1 far above candidate 0, so it won.
        assert outcome.selected.index == 1
    # Generation on the base model, judging on the judge model's own quota
    # bucket (claude_context.md §4, §8 mitigation #1). The first
    # 2 * N_CHUNKS calls are generation (2 variants x N_CHUNKS chunk calls
    # each); the judge starts right after.
    assert client.calls[0]["model"] == "qwen/qwen3.8-27b"
    assert client.calls[2 * N_CHUNKS]["model"] == "openai/gpt-oss-20b"


async def test_heuristic_backend_costs_no_scoring_calls(fake_conn, fake_storage) -> None:
    """Two of the three scorer heads are free, which is what makes a 4-arm
    ablation affordable (§8 mitigation #2)."""
    _, judged = await run(
        fake_conn, fake_storage,
        rc=run_config(n_candidates=2, k_iterations=1, scorer_backend="both"),
        turns=turns_for(2, 1, judge=True),
    )
    _, free = await run(
        fake_conn, fake_storage,
        rc=run_config(n_candidates=2, k_iterations=1, scorer_backend="heuristic"),
        turns=turns_for(2, 1),
    )
    assert len(judged.calls) - len(free.calls) == N_FIELDS, "one judge call per field"


async def test_scorer_weights_come_from_the_run_config(fake_conn, fake_storage) -> None:
    """Weights are recorded per run, so a weighting change is a new arm rather
    than an edit to the code that produced the last one."""
    from coda.v1 import runconfig_pb2

    rc = run_config(
        n_candidates=1,
        k_iterations=1,
        scorer_weights=runconfig_pb2.ScorerWeights(
            relevance=1.0, consistency=0.0, redundancy=0.0
        ),
    )
    result, _ = await run(fake_conn, fake_storage, rc=rc, turns=turns_for(1, 1))
    cc = result.outcomes["chief_complaint"]
    # With consistency and redundancy zeroed, the aggregate is pure relevance.
    assert cc.score.aggregate == pytest.approx(cc.score.relevance)


# --------------------------------------------------------- cost accounting


async def test_generation_cost_is_apportioned_across_fields_not_charged_to_each(
    fake_conn, fake_storage
) -> None:
    """One batched call covers every field, so charging its full cost to each
    field would report ~9x the tokens actually spent — and the GoT-vs-baseline
    cost comparison (Phase 6 AC5) is a headline number."""
    result, _ = await run(
        fake_conn, fake_storage,
        rc=run_config(n_candidates=1, k_iterations=1),
        turns=turns_for(1, 1, gen_tokens=(900, 180)),
    )
    per_field = result.outcomes["chief_complaint"]
    # Refinement is genuinely per-field and exact; generation's share is the
    # even split. With zero-token refinement turns the split is all there is.
    # `gen_tokens` is charged to every one of the N_CHUNKS chunk calls one
    # variant costs, so the true generation total is `900 * N_CHUNKS`.
    assert per_field.tokens_in == 900 * N_CHUNKS // N_FIELDS
    assert per_field.tokens_out == 180 * N_CHUNKS // N_FIELDS
    # Apportioning never inflates the total past what was really spent.
    assert sum(result.per_field_tokens.values()) <= result.tokens_in + result.tokens_out
    assert result.tokens_in == 900 * N_CHUNKS
    assert result.tokens_out == 180 * N_CHUNKS
    assert result.llm_calls == N_CHUNKS + N_FIELDS + 1


async def test_per_field_tokens_are_reported_for_every_field(fake_conn, fake_storage) -> None:
    result, _ = await run(
        fake_conn, fake_storage,
        rc=run_config(n_candidates=1, k_iterations=1),
        turns=turns_for(1, 1, gen_tokens=(900, 180)),
    )
    assert set(result.per_field_tokens) == set(result.outcomes)
    assert all(v > 0 for v in result.per_field_tokens.values())


async def test_a_failed_variant_is_reported_on_the_result(fake_conn, fake_storage) -> None:
    """A degraded pool must be visible to the caller, so a run that quietly
    became N=1 is not read as an N=2 result."""
    # Variant 1 ("conservative"): all N_CHUNKS chunk calls succeed. Variant 2
    # ("inclusive"): its FIRST chunk call fails all `repair_max_attempts + 1`
    # attempts (`run()` passes repair_max_attempts=2, so 3 "not json"
    # turns) — the whole variant fails right there (see generate_candidates'
    # `variant_failed` break), so chunks 2 and 3 for variant 2 are never
    # attempted and need no scripted turns.
    turns = [
        CassetteTurn(content=generation_response(chunk))
        for chunk in chunk_fields(list(FIELDS), FIELDS_PER_GENERATION_CALL)
    ] + [
        CassetteTurn(content="not json"),
        CassetteTurn(content="not json"),
        CassetteTurn(content="not json"),
    ]
    for field_key in FIELDS:
        turns.append(CassetteTurn(content=critique_response(field_key)))
    turns.append(CassetteTurn(content=SUMMARY))
    result, _ = await run(
        fake_conn, fake_storage,
        rc=run_config(n_candidates=2, k_iterations=1), turns=turns,
    )
    assert result.failed_variants == ["inclusive"]
    assert result.repair_attempts >= 2
