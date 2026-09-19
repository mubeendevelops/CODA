"""Module 5's scorer — GoT-HCS Eq. 19-22's intent without trained heads.

The properties under test are the ones the substitution rests on: each
criterion measures what its name says, the sign convention holds, the
contradiction penalty catches the polarity failure the whole design exists
for, and the heuristic scorer genuinely costs nothing.
"""

from __future__ import annotations

import inspect
import json

import pytest
from factories import Cat, Pol, candidate, thought

from coda.v1 import runconfig_pb2
from nlp_service.llm.cassette import CassetteLLMClient, CassetteTurn
from nlp_service.reasoning import scoring
from nlp_service.reasoning.embeddings import LexicalEmbeddingBackend
from nlp_service.reasoning.scoring import (
    DEFAULT_WEIGHTS,
    ScoringContext,
    aggregate,
    contradiction_penalty,
    redundancy_score,
    relevance_score,
    resolve_weights,
    score_heuristic,
)

W = DEFAULT_WEIGHTS


# --------------------------------------------------------------------------
# Recorded defaults
# --------------------------------------------------------------------------


def test_default_weights_are_the_recorded_values() -> None:
    """Decision #95. No defaults existed anywhere before 2026-09-04; pinning
    them here means a silent change shows up as a test failure rather than as
    an unexplained shift in every ablation number."""
    assert (W.relevance, W.consistency, W.redundancy) == pytest.approx((0.5, 0.3, 0.2))
    assert W.relevance + W.consistency + W.redundancy == pytest.approx(1.0)
    assert W.relevance > W.consistency > W.redundancy


def test_unset_weights_fall_back_to_defaults() -> None:
    """proto3 cannot distinguish 'unset' from 'deliberately all zero', and an
    all-zero triple has no selection criterion at all, so it is treated as
    unset."""
    rc = runconfig_pb2.RunConfig()
    assert resolve_weights(rc) == W


def test_explicit_weights_are_respected() -> None:
    rc = runconfig_pb2.RunConfig(
        scorer_weights=runconfig_pb2.ScorerWeights(
            relevance=0.8, consistency=0.1, redundancy=0.1
        )
    )
    assert resolve_weights(rc).relevance == pytest.approx(0.8)


# --------------------------------------------------------------------------
# Sign convention
# --------------------------------------------------------------------------


def test_redundancy_is_subtracted_not_added() -> None:
    """Decision #95: `redundancy` stores RAW overlap and is higher-is-worse,
    so the aggregate subtracts it. Getting this backwards would make the
    scorer actively prefer restatement."""
    clean = aggregate(relevance=0.8, consistency=0.8, redundancy=0.0, weights=W)
    repetitive = aggregate(relevance=0.8, consistency=0.8, redundancy=1.0, weights=W)
    assert clean > repetitive
    assert clean - repetitive == pytest.approx(W.redundancy)


def test_aggregate_matches_the_documented_formula() -> None:
    got = aggregate(relevance=0.6, consistency=0.4, redundancy=0.25, weights=W)
    expected = 0.5 * 0.6 + 0.3 * 0.4 - 0.2 * 0.25
    assert got == pytest.approx(expected)


# --------------------------------------------------------------------------
# Relevance
# --------------------------------------------------------------------------


def test_relevance_is_coverage_of_the_subgraph_entities() -> None:
    ctx = ScoringContext(
        field_key="hopi",
        thoughts=[
            thought("t1", 1, "Cough for three days", ["cough"]),
            thought("t2", 5, "Shortness of breath", ["shortness of breath"]),
        ],
    )
    assert relevance_score("Cough for three days", ctx) == pytest.approx(0.5)
    assert relevance_score(
        "Cough for three days with shortness of breath", ctx
    ) == pytest.approx(1.0)
    assert relevance_score("Patient seems well", ctx) == pytest.approx(0.0)


def test_relevance_does_not_penalize_extra_words() -> None:
    """Recall-oriented, not Jaccard: penalizing length would push the scorer
    toward sparse answers, which is the opposite of what a clinical note
    needs."""
    ctx = ScoringContext(
        field_key="hopi", thoughts=[thought("t1", 1, "Cough", ["cough"])]
    )
    terse = relevance_score("Cough.", ctx)
    detailed = relevance_score(
        "Cough, described as dry, worse at night, ongoing for three weeks.", ctx
    )
    assert terse == detailed == pytest.approx(1.0)


def test_relevance_is_zero_when_the_subgraph_names_no_entities() -> None:
    """Nothing to be relevant *to* — a candidate should not be rewarded for
    a field whose evidence establishes nothing."""
    ctx = ScoringContext(field_key="hopi", thoughts=[thought("t1", 1, "Hmm", [])])
    assert relevance_score("Anything at all", ctx) == 0.0


# --------------------------------------------------------------------------
# Consistency and the contradiction penalty
# --------------------------------------------------------------------------


def test_contradiction_penalty_fires_on_asserting_a_negated_thought(
    lexical_backend: LexicalEmbeddingBackend,
) -> None:
    """The failure the whole polarity design exists to catch: the evidence
    says the allergy was disproved, the candidate writes it as fact."""
    ctx = ScoringContext(
        field_key="allergies",
        thoughts=[
            thought(
                "t1",
                9,
                "No penicillin allergy documented in the record",
                ["penicillin", "allergy"],
                category=Cat.THOUGHT_CATEGORY_ALLERGY,
                polarity=Pol.POLARITY_NEGATED,
            )
        ],
    )
    assert contradiction_penalty("Penicillin allergy", ctx) == scoring.CONTRADICTION_PENALTY

    bad = score_heuristic(candidate("Penicillin allergy"), ctx, weights=W, backend=lexical_backend)
    good = score_heuristic(
        candidate("No documented penicillin allergy"), ctx, weights=W, backend=lexical_backend
    )
    assert bad.contradiction_penalty == scoring.CONTRADICTION_PENALTY
    assert good.contradiction_penalty == 0.0
    assert good.aggregate > bad.aggregate


def test_correctly_recorded_pertinent_negative_is_not_penalized() -> None:
    """The check must be narrow. A false positive would suppress a candidate
    that correctly preserves a pertinent negative — exactly the content
    clinicians most want kept."""
    ctx = ScoringContext(
        field_key="past_medical_history",
        thoughts=[
            thought(
                "t1",
                3,
                "No recent travel",
                ["travel"],
                category=Cat.THOUGHT_CATEGORY_HISTORY,
                polarity=Pol.POLARITY_NEGATED,
            )
        ],
    )
    for phrasing in (
        "No recent travel",
        "Denies recent travel",
        "Travel history: negative",
        "Patient reports no travel abroad",
    ):
        assert contradiction_penalty(phrasing, ctx) == 0.0, phrasing


def test_no_penalty_when_the_subgraph_has_no_negated_thought() -> None:
    ctx = ScoringContext(
        field_key="hopi", thoughts=[thought("t1", 1, "Cough for three days", ["cough"])]
    )
    assert contradiction_penalty("Cough for three days", ctx) == 0.0


def test_consistency_uses_max_not_mean_over_supporting_thoughts(
    lexical_backend: LexicalEmbeddingBackend,
) -> None:
    """A field's subgraph legitimately covers different aspects; a good
    candidate is strongly grounded in SOME of them, and a mean would punish
    it for the ones it does not restate."""
    ctx = ScoringContext(
        field_key="hopi",
        thoughts=[
            thought("t1", 1, "Cough for three weeks", ["cough"]),
            thought("t2", 7, "Gave up smoking eight years ago", ["smoking"]),
        ],
    )
    con, _ = scoring.consistency_score("Cough for three weeks", ctx, backend=lexical_backend)
    sims = lexical_backend.similarity("Cough for three weeks", [t.text for t in ctx.thoughts])
    assert con == pytest.approx(max(sims))
    assert max(sims) > sum(sims) / len(sims)


def test_consistency_reports_its_backend(lexical_backend: LexicalEmbeddingBackend) -> None:
    """Decision #97: a lexical score must never silently claim to be an
    embedding similarity."""
    ctx = ScoringContext(field_key="hopi", thoughts=[thought("t1", 1, "Cough", ["cough"])])
    breakdown = score_heuristic(candidate("Cough"), ctx, weights=W, backend=lexical_backend)
    assert breakdown.consistency_backend == "lexical"
    assert breakdown.scorer_backend == scoring.SCORER_HEURISTIC


# --------------------------------------------------------------------------
# Redundancy
# --------------------------------------------------------------------------


def test_redundancy_measures_overlap_with_other_fields_only() -> None:
    ctx_fresh = ScoringContext(field_key="treatment_plan", thoughts=[], already_selected=[])
    assert redundancy_score("Amoxicillin 500mg three times daily", ctx_fresh) == 0.0

    ctx_repeat = ScoringContext(
        field_key="treatment_plan",
        thoughts=[],
        already_selected=["Amoxicillin 500mg three times daily"],
    )
    assert redundancy_score("Amoxicillin 500mg three times daily", ctx_repeat) == pytest.approx(1.0)


def test_redundancy_is_proportional_to_the_candidate_not_absolute() -> None:
    """Measured as the fraction of the CANDIDATE that is restatement, so a
    long candidate is not penalized merely for being long."""
    ctx = ScoringContext(
        field_key="treatment_plan", thoughts=[], already_selected=["start amoxicillin today"]
    )
    short = redundancy_score("start amoxicillin today", ctx)
    long = redundancy_score(
        "start amoxicillin today and return in one week if the fever has not settled "
        "or if new breathlessness develops",
        ctx,
    )
    assert short > long


# --------------------------------------------------------------------------
# The zero-API guarantee
# --------------------------------------------------------------------------


def test_heuristic_scorer_takes_no_client_and_no_connection() -> None:
    """claude_context.md §8's mitigation #2 — two of three criteria cost no
    API calls — is what makes a 4-arm ablation affordable. The strongest form
    this assertion can take is that the function cannot make a call: it is
    not async and accepts neither a connection nor a client.
    """
    sig = inspect.signature(score_heuristic)
    assert not inspect.iscoroutinefunction(score_heuristic)
    assert "conn" not in sig.parameters
    assert "llm_client" not in sig.parameters


# --------------------------------------------------------------------------
# The LLM judge
# --------------------------------------------------------------------------


async def test_llm_judge_scores_every_candidate_in_one_call(fake_conn: object) -> None:
    """N separate calls would cost N times as much AND produce independently
    calibrated scores, which is worse for a task whose only question is
    'which of these is best'."""
    cands = [candidate("a", index=0), candidate("b", index=1), candidate("c", index=2)]
    payload = {
        "scores": [
            {"index": 0, "relevance": 0.9, "consistency": 0.9, "redundancy": 0.1,
             "rationale": "covers the evidence"},
            {"index": 1, "relevance": 0.5, "consistency": 0.8, "redundancy": 0.2,
             "rationale": "misses duration"},
            {"index": 2, "relevance": 0.2, "consistency": 0.3, "redundancy": 0.6,
             "rationale": "largely restates the plan"},
        ]
    }
    client = CassetteLLMClient(turns=[CassetteTurn(content=json.dumps(payload))])
    ctx = ScoringContext(field_key="hopi", thoughts=[thought("t1", 1, "Cough", ["cough"])])

    breakdowns, t_in, t_out, calls, hits = await scoring.score_llm_judge(
        cands,
        ctx,
        conn=fake_conn,  # type: ignore[arg-type]
        llm_client=client,
        judge_model="openai/gpt-oss-20b",
        weights=W,
        timeout_s=5.0,
    )
    assert calls == 1
    assert len(breakdowns) == 3
    assert all(b.scorer_backend == scoring.SCORER_LLM_JUDGE for b in breakdowns)
    assert all(b.judge_model == "openai/gpt-oss-20b" for b in breakdowns)
    assert breakdowns[0].aggregate > breakdowns[2].aggregate
    assert breakdowns[0].judge_rationale


async def test_llm_judge_requests_enough_tokens_for_its_reasoning(fake_conn: object) -> None:
    """Decision #75's failure: a reasoning judge can spend its whole output
    budget on hidden reasoning and emit empty content."""
    payload = {"scores": [{"index": 0, "relevance": 0.5, "consistency": 0.5,
                           "redundancy": 0.0, "rationale": "ok"}]}
    client = CassetteLLMClient(turns=[CassetteTurn(content=json.dumps(payload))])
    ctx = ScoringContext(field_key="hopi", thoughts=[thought("t1", 1, "Cough", ["cough"])])
    await scoring.score_llm_judge(
        [candidate("a")],
        ctx,
        conn=fake_conn,  # type: ignore[arg-type]
        llm_client=client,
        judge_model="openai/gpt-oss-20b",
        weights=W,
        timeout_s=5.0,
    )
    assert client.calls[0]["max_tokens"] is not None
    assert int(client.calls[0]["max_tokens"]) >= 1500  # type: ignore[arg-type]


async def test_unusable_judge_output_degrades_rather_than_raising(fake_conn: object) -> None:
    """Scoring is an ordering mechanism. If the judge is unavailable,
    selection should fall back to the free scorer, not fail a whole
    consultation that already has candidates."""
    client = CassetteLLMClient(turns=[CassetteTurn(content="not json at all")])
    ctx = ScoringContext(field_key="hopi", thoughts=[thought("t1", 1, "Cough", ["cough"])])
    breakdowns, *_ = await scoring.score_llm_judge(
        [candidate("a"), candidate("b", index=1)],
        ctx,
        conn=fake_conn,  # type: ignore[arg-type]
        llm_client=client,
        judge_model="openai/gpt-oss-20b",
        weights=W,
        timeout_s=5.0,
    )
    assert len(breakdowns) == 2
    assert all(b.aggregate == 0.0 for b in breakdowns)
    assert all("unusable" in b.judge_rationale for b in breakdowns)


async def test_judge_with_no_candidates_makes_no_call(fake_conn: object) -> None:
    client = CassetteLLMClient(turns=[])
    ctx = ScoringContext(field_key="hopi", thoughts=[])
    breakdowns, _, _, calls, _ = await scoring.score_llm_judge(
        [],
        ctx,
        conn=fake_conn,  # type: ignore[arg-type]
        llm_client=client,
        judge_model="m",
        weights=W,
        timeout_s=5.0,
    )
    assert breakdowns == [] and calls == 0 and client.calls == []
