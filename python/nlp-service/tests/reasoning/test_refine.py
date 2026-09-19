"""Module 5's select-and-refine loop.

The property that matters most here is the **regression guard**. Refinement
is the stage most likely to quietly make the delivered note worse: the
critique prompt asks the model to find fault, and a model asked to find fault
will sometimes invent one. The guard means a worse revision is recorded in
the trace but not adopted — so `got_k1` vs `got_k2` still measures whether
refinement helps, while a bad refinement never reaches the doctor.

The second property is that the **trajectory records attempts, not just
adoptions**. A trajectory that only ever went up would be evidence of nothing.
"""

from __future__ import annotations

import json

import pytest
from factories import Pol, candidate, thought

from coda.v1 import got_pb2, runconfig_pb2
from nlp_service.llm.cassette import CassetteLLMClient, CassetteTurn
from nlp_service.reasoning import scoring
from nlp_service.reasoning.refine import refine_field, render_subgraph, select_best

WEIGHTS = runconfig_pb2.ScorerWeights(relevance=0.5, consistency=0.3, redundancy=0.2)


def score(aggregate: float) -> got_pb2.ScoreBreakdown:
    return got_pb2.ScoreBreakdown(aggregate=aggregate, scorer_backend="heuristic")


def critique_response(*, value=None, items=None, ids=None, critique="tighten it") -> str:
    return json.dumps(
        {
            "critique": critique,
            "revised": {
                "value": value,
                "items": items or [],
                "source_thought_ids": ids or ["t1"],
                "confidence": 0.9,
            },
        }
    )


def subgraph() -> list:
    return [
        thought("t1", 1, "chest pain radiating to the left arm", ["chest pain", "left arm"]),
        thought("t2", 3, "worse on exertion", ["exertion"]),
    ]


# ------------------------------------------------------------------ selection


def test_select_best_takes_the_highest_aggregate() -> None:
    cands = [candidate("a", index=0), candidate("b", index=1), candidate("c", index=2)]
    assert select_best(cands, [score(0.1), score(0.7), score(0.4)]) == 1


def test_ties_break_toward_the_conservative_variant() -> None:
    """Deterministic by construction. architecture.md §6.3 requires the run be
    reconstructible, and an arbitrary tie-break would not be — two identical
    replays could select different candidates and produce different notes."""
    cands = [candidate("a", index=0), candidate("b", index=1)]
    assert select_best(cands, [score(0.5), score(0.5)]) == 0
    # Repeated calls agree, which is the actual reconstructibility claim.
    assert {select_best(cands, [score(0.5), score(0.5)]) for _ in range(20)} == {0}


def test_select_best_with_no_candidates_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        select_best([], [])


# ---------------------------------------------------------------- the K loop


async def test_k_zero_runs_no_refinement_call(fake_conn, lexical_backend) -> None:
    """The K=0 control must cost nothing, not merely change the output."""
    client = CassetteLLMClient(turns=[])
    ctx = scoring.ScoringContext(field_key="chief_complaint", thoughts=subgraph())
    original = candidate("chest pain", source_thought_ids=["t1"])
    result = await refine_field(
        conn=fake_conn,
        llm_client=client,
        model="m",
        field_key="chief_complaint",
        thoughts=subgraph(),
        candidate=original,
        score=score(0.5),
        ctx=ctx,
        weights=WEIGHTS,
        embed_backend=lexical_backend,
        k_iterations=0,
        temperature=0.0,
        repair_max_attempts=2,
        timeout_s=5.0,
    )
    selected, _, sets, _, t_in, t_out, calls, _ = result
    assert client.calls == []
    assert calls == 0 and t_in == 0 and t_out == 0
    assert sets == []
    assert selected is original


async def test_an_improving_revision_is_adopted(fake_conn, lexical_backend) -> None:
    """A revision that covers more of the subgraph's entities scores higher on
    relevance and should replace the original."""
    ctx = scoring.ScoringContext(field_key="chief_complaint", thoughts=subgraph())
    weak = candidate("pain", source_thought_ids=["t1"])
    weak_score = scoring.score_heuristic(weak, ctx, weights=WEIGHTS, backend=lexical_backend)
    client = CassetteLLMClient(
        turns=[
            CassetteTurn(
                content=critique_response(
                    value="chest pain radiating to the left arm, worse on exertion",
                    ids=["t1", "t2"],
                )
            )
        ]
    )
    selected, final_score, sets, confidence, *_ = await refine_field(
        conn=fake_conn,
        llm_client=client,
        model="m",
        field_key="chief_complaint",
        thoughts=subgraph(),
        candidate=weak,
        score=weak_score,
        ctx=ctx,
        weights=WEIGHTS,
        embed_backend=lexical_backend,
        k_iterations=1,
        temperature=0.0,
        repair_max_attempts=2,
        timeout_s=5.0,
    )
    assert selected.text.startswith("chest pain radiating")
    assert selected.is_refinement is True
    assert selected.variant.endswith("+refine1")
    assert final_score.aggregate > weak_score.aggregate
    assert confidence == pytest.approx(0.9)
    assert len(sets) == 1 and sets[0].iteration == 1


async def test_a_worse_revision_is_recorded_but_not_adopted(fake_conn, lexical_backend) -> None:
    """The regression guard. The trace must still show the attempt — that is
    how 'K=2 sometimes hurts' stays measurable — while the delivered value
    keeps the better text."""
    ctx = scoring.ScoringContext(field_key="chief_complaint", thoughts=subgraph())
    strong = candidate(
        "chest pain radiating to the left arm, worse on exertion",
        source_thought_ids=["t1", "t2"],
    )
    strong_score = scoring.score_heuristic(strong, ctx, weights=WEIGHTS, backend=lexical_backend)
    client = CassetteLLMClient(
        turns=[CassetteTurn(content=critique_response(value="pain", ids=["t1"]))]
    )
    selected, final_score, sets, *_ = await refine_field(
        conn=fake_conn,
        llm_client=client,
        model="m",
        field_key="chief_complaint",
        thoughts=subgraph(),
        candidate=strong,
        score=strong_score,
        ctx=ctx,
        weights=WEIGHTS,
        embed_backend=lexical_backend,
        k_iterations=1,
        temperature=0.0,
        repair_max_attempts=2,
        timeout_s=5.0,
    )
    # Not adopted...
    assert selected is strong
    assert final_score.aggregate == strong_score.aggregate
    # ...but recorded, with its (worse) score, so the trajectory shows it.
    assert len(sets) == 1
    assert sets[0].candidates[0].text == "pain"
    assert sets[0].scores[0].aggregate < strong_score.aggregate


async def test_k_two_issues_two_calls_and_each_sees_the_current_candidate(
    fake_conn, lexical_backend
) -> None:
    """Each pass must critique what survived the last one, not the original —
    otherwise K=2 is two independent K=1 passes and the loop means nothing."""
    ctx = scoring.ScoringContext(field_key="chief_complaint", thoughts=subgraph())
    weak = candidate("pain", source_thought_ids=["t1"])
    weak_score = scoring.score_heuristic(weak, ctx, weights=WEIGHTS, backend=lexical_backend)
    client = CassetteLLMClient(
        turns=[
            CassetteTurn(content=critique_response(value="chest pain", ids=["t1"])),
            CassetteTurn(
                content=critique_response(
                    value="chest pain radiating to the left arm, worse on exertion",
                    ids=["t1", "t2"],
                )
            ),
        ]
    )
    selected, _, sets, *_ = await refine_field(
        conn=fake_conn,
        llm_client=client,
        model="m",
        field_key="chief_complaint",
        thoughts=subgraph(),
        candidate=weak,
        score=weak_score,
        ctx=ctx,
        weights=WEIGHTS,
        embed_backend=lexical_backend,
        k_iterations=2,
        temperature=0.0,
        repair_max_attempts=2,
        timeout_s=5.0,
    )
    assert len(client.calls) == 2
    assert [s.iteration for s in sets] == [1, 2]
    # Pass 2's prompt carries pass 1's adopted output, not the original.
    second = client.calls[1]["user_prompt"]
    assert "chest pain" in second
    assert selected.text.startswith("chest pain radiating")


async def test_an_unfixable_revision_stops_the_loop_rather_than_burning_k(
    fake_conn, lexical_backend
) -> None:
    """If the model could not produce a valid revision once, spending the
    remaining K on it is unlikely to help and definitely costs quota."""
    ctx = scoring.ScoringContext(field_key="chief_complaint", thoughts=subgraph())
    original = candidate("chest pain", source_thought_ids=["t1"])
    client = CassetteLLMClient(turns=[CassetteTurn(content="not json")] * 8)
    selected, _, sets, _, _, _, calls, _ = await refine_field(
        conn=fake_conn,
        llm_client=client,
        model="m",
        field_key="chief_complaint",
        thoughts=subgraph(),
        candidate=original,
        score=score(0.5),
        ctx=ctx,
        weights=WEIGHTS,
        embed_backend=lexical_backend,
        k_iterations=3,
        temperature=0.0,
        repair_max_attempts=1,
        timeout_s=5.0,
    )
    assert selected is original
    assert sets == []
    # Iteration 1 spent its repair budget (2 calls) and then stopped; it did
    # not go on to spend iterations 2 and 3.
    assert calls == 2


async def test_a_revision_citing_an_unknown_thought_is_refused(
    fake_conn, lexical_backend
) -> None:
    """Provenance survives refinement. A revision may reword the value; it may
    not invent evidence for it."""
    ctx = scoring.ScoringContext(field_key="chief_complaint", thoughts=subgraph())
    original = candidate("chest pain", source_thought_ids=["t1"])
    client = CassetteLLMClient(
        turns=[CassetteTurn(content=critique_response(value="anything", ids=["t-invented"]))] * 4
    )
    selected, _, sets, *_ = await refine_field(
        conn=fake_conn,
        llm_client=client,
        model="m",
        field_key="chief_complaint",
        thoughts=subgraph(),
        candidate=original,
        score=score(0.5),
        ctx=ctx,
        weights=WEIGHTS,
        embed_backend=lexical_backend,
        k_iterations=1,
        temperature=0.0,
        repair_max_attempts=1,
        timeout_s=5.0,
    )
    assert selected is original
    assert sets == []


async def test_list_field_revisions_keep_their_items(fake_conn, lexical_backend) -> None:
    ctx = scoring.ScoringContext(field_key="allergies", thoughts=subgraph())
    original = candidate("penicillin", items=["penicillin"], source_thought_ids=["t1"])
    client = CassetteLLMClient(
        turns=[
            CassetteTurn(
                content=critique_response(items=["penicillin", "sulfa"], ids=["t1", "t2"])
            )
        ]
    )
    selected, *_ = await refine_field(
        conn=fake_conn,
        llm_client=client,
        model="m",
        field_key="allergies",
        thoughts=subgraph(),
        candidate=original,
        score=score(0.0),
        ctx=ctx,
        weights=WEIGHTS,
        embed_backend=lexical_backend,
        k_iterations=1,
        temperature=0.0,
        repair_max_attempts=2,
        timeout_s=5.0,
    )
    assert list(selected.items) == ["penicillin", "sulfa"]
    assert selected.text == "penicillin, sulfa"


# ------------------------------------------------------------------ rendering


def test_the_critique_prompt_shows_polarity() -> None:
    """The refiner has to be able to see that a thought was negated — this is
    the dialogue-specific signal (decision #89) the whole graph exists to
    carry, and dropping it here would silently undo it at the last step."""
    thoughts = [
        thought("t1", 1, "chest pain", ["chest pain"]),
        thought("t2", 9, "no chest pain on review", ["chest pain"], polarity=Pol.POLARITY_NEGATED),
    ]
    rendered = render_subgraph(thoughts)
    assert "[turn 1, asserted]" in rendered
    assert "[turn 9, negated]" in rendered


def test_an_empty_subgraph_says_so() -> None:
    assert "no supporting thoughts" in render_subgraph([])
