"""Module 5's candidate generation.

The two properties worth pinning here are the ones the design argues for at
length and that nothing else would catch:

- candidates differ because they were shown **different subgraphs and asked
  different questions**, not because a sampler was run hot. A regression to
  temperature-sampling would still produce N candidates and still pass any
  count-based assertion, so the difference is asserted on the *contexts*;
- generation is **batched across fields** — N calls, not 8N. That is
  claude_context.md §8's mitigation #4 and the single largest determinant of
  whether a 4-arm ablation fits in the free tier.
"""

from __future__ import annotations

import json

import pytest
from factories import Cat, EdgeT, edge, thought

from coda_worker_sdk.errors import FatalError
from nlp_service.llm.cassette import CassetteLLMClient, CassetteTurn
from nlp_service.reasoning import generation
from nlp_service.reasoning.generation import (
    LIST_FIELDS,
    VARIANTS,
    build_variant_contexts,
    full_transcript_contexts,
    generate_candidates,
    render_field_blocks,
    variants_for,
)

FIELDS = ["chief_complaint", "past_medical_history", "allergies"]


def graph_thoughts() -> list:
    """A small graph with one thought per seeded category, chained so depth
    actually reaches something.

    Only `t1` seeds `chief_complaint`; `t2` and `t3` are reachable from it
    only by traversal, one hop apart. That is what makes depth 1 and depth 2
    retrieve genuinely different sets — if every thought were a seed, both
    variants would see everything and the diversity assertion below would
    pass without meaning anything.
    """
    return [
        thought("t1", 1, "chest pain for two days", ["chest pain"]),
        thought(
            "t2",
            3,
            "chest is tender on palpation",
            ["tenderness"],
            category=Cat.THOUGHT_CATEGORY_EXAMINATION,
        ),
        thought(
            "t3",
            5,
            "ECG ordered to rule out ischaemia",
            ["ecg"],
            category=Cat.THOUGHT_CATEGORY_INVESTIGATION,
        ),
        thought(
            "t4", 2, "diabetes for ten years", ["diabetes"], category=Cat.THOUGHT_CATEGORY_HISTORY
        ),
        thought(
            "t5", 7, "penicillin allergy", ["penicillin"], category=Cat.THOUGHT_CATEGORY_ALLERGY
        ),
    ]


def graph_edges() -> list:
    return [
        edge("t1", "t2", EdgeT.EDGE_TYPE_ELABORATION, 0.9),
        edge("t2", "t3", EdgeT.EDGE_TYPE_ELABORATION, 0.9),
        edge("t1", "t4", EdgeT.EDGE_TYPE_TEMPORAL, 0.3),
    ]


def payload(fields: dict[str, dict]) -> str:
    return json.dumps({"fields": fields})


def field_payload(*, value=None, items=None, ids=None, confidence=0.8) -> dict:
    return {
        "value": value,
        "items": items or [],
        "source_thought_ids": ids or [],
        "confidence": confidence,
    }


def valid_response(field_key: str, ids: list[str] | None = None) -> str:
    """A valid response for exactly ONE field — one generation call's worth,
    now that a call covers `FIELDS_PER_GENERATION_CALL` (currently 1) field
    rather than every field at once."""
    if field_key == "chief_complaint":
        return payload({field_key: field_payload(value="chest pain", ids=ids or ["t1"])})
    if field_key == "past_medical_history":
        return payload({field_key: field_payload(items=["diabetes"], ids=["t4"])})
    if field_key == "allergies":
        return payload({field_key: field_payload(items=["penicillin"], ids=["t5"])})
    return payload({field_key: field_payload()})


# --------------------------------------------------------------- variant pool


def test_variants_are_distinct_views_not_repeated_samples() -> None:
    """N=3 uses all three named variants, each with its own retrieval policy.

    If this ever collapses to one variant repeated, candidate diversity is
    coming from sampling noise and the scorer is choosing between rewordings.
    """
    got = variants_for(3)
    assert [v.name for v in got] == ["conservative", "inclusive", "negative_aware"]
    assert len({v.instruction for v in got}) == 3
    assert len({json.dumps(v.policy_overrides, sort_keys=True) for v in got}) >= 2


def test_n_of_one_uses_the_conservative_variant() -> None:
    """The N=1 control arm must be the *conservative* view, not an arbitrary
    one — it is the arm the ablation reads as 'no candidate competition'."""
    assert [v.name for v in variants_for(1)] == ["conservative"]


def test_beyond_the_pool_variants_widen_instead_of_duplicating() -> None:
    """N=5 must be five different views. Two duplicates would make candidates
    3 and 4 identical to 0 and 1, and the scorer would be picking between
    copies while the run paid for both."""
    got = variants_for(5)
    assert len(got) == 5
    depths = [v.policy_overrides.get("max_depth") for v in got]
    assert depths[3] != depths[0], "the first repeat must widen its depth"
    assert depths[4] != depths[1]
    assert len({v.name for v in got}) == 5


def test_zero_candidates_is_rejected() -> None:
    with pytest.raises(ValueError, match="n_candidates must be >= 1"):
        variants_for(0)


# ------------------------------------------------------------------- contexts


def test_variants_really_see_different_subgraphs() -> None:
    """The claim the whole design rests on, asserted on the retrieved sets
    rather than on the prompt text."""
    thoughts, edges = graph_thoughts(), graph_edges()
    tight = build_variant_contexts(
        variant=VARIANTS[0], field_keys=FIELDS, thoughts=thoughts, edges=edges, context_tokens=300
    )
    wide = build_variant_contexts(
        variant=VARIANTS[1], field_keys=FIELDS, thoughts=thoughts, edges=edges, context_tokens=300
    )
    tight_cc = {t.id for t in tight.contexts["chief_complaint"].thoughts}
    wide_cc = {t.id for t in wide.contexts["chief_complaint"].thoughts}
    # depth 1 vs depth 2 over t1 -> t2 -> t3.
    assert tight_cc < wide_cc
    assert "t3" in wide_cc and "t3" not in tight_cc


def test_nograph_arm_gives_every_field_every_thought() -> None:
    """`graph_context_enabled = false` is the retrieval control. It must
    differ from the graph arm ONLY in which thoughts reach the prompt."""
    thoughts = graph_thoughts()
    fc = full_transcript_contexts(field_keys=FIELDS, thoughts=thoughts, variant=VARIANTS[0])
    for field_key in FIELDS:
        assert {t.id for t in fc.contexts[field_key].thoughts} == {t.id for t in thoughts}
    # Chronological, so the prompt reads like the consultation happened.
    ordered = fc.contexts["chief_complaint"].thoughts
    assert [t.turn_index for t in ordered] == sorted(t.turn_index for t in thoughts)


def test_field_blocks_declare_shape_and_name_the_empty_case() -> None:
    """The model must be told which fields are lists, and an empty field must
    say so explicitly — an unexplained missing block invites invention."""
    thoughts = graph_thoughts()
    fc = build_variant_contexts(
        variant=VARIANTS[0],
        field_keys=["chief_complaint", "allergies", "medications"],
        thoughts=thoughts,
        edges=graph_edges(),
        context_tokens=300,
    )
    rendered = render_field_blocks(fc.contexts)
    assert "### chief_complaint (scalar)" in rendered
    assert "### allergies (list-valued)" in rendered
    # No MEDICATION thought exists in the fixture, so this field is empty.
    assert "### medications (list-valued)" in rendered
    assert "no supporting thoughts were retrieved" in rendered
    assert "allergies" in LIST_FIELDS


# ----------------------------------------------------------------- generation


async def test_generation_issues_one_call_per_field_per_variant(fake_conn) -> None:
    """N x len(FIELDS) calls, not N. §8 mitigation #4 originally batched every
    field into one call per variant; a real OTPM ceiling on this model's
    output tokens/minute made that impossible for real (full-UUID) citations
    (found live 2026-09-05, see generation.FIELDS_PER_GENERATION_CALL's
    docstring) — this pins the current, smaller batch size rather than the
    call count the design started with."""
    client = CassetteLLMClient(
        turns=[
            CassetteTurn(content=valid_response(fk)) for _ in range(3) for fk in FIELDS
        ]
    )
    result = await generate_candidates(
        conn=fake_conn,
        llm_client=client,
        model="qwen/qwen3.8-27b",
        field_keys=FIELDS,
        thoughts=graph_thoughts(),
        edges=graph_edges(),
        n_candidates=3,
        graph_context_enabled=True,
        context_tokens=300,
        temperature=0.0,
        repair_max_attempts=2,
        timeout_s=5.0,
    )
    assert len(client.calls) == 3 * len(FIELDS)
    assert result.llm_calls == 3 * len(FIELDS)
    for field_key in FIELDS:
        assert len(result.candidates_by_field[field_key]) == 3


async def test_every_candidate_carries_its_variant_and_cited_thoughts(fake_conn) -> None:
    """The persisted provenance the case-study figures read: which variant
    produced this, and which thoughts it claims to rest on."""
    client = CassetteLLMClient(
        turns=[
            CassetteTurn(content=valid_response(fk, ids=["t1", "t2"]))
            for _ in range(3)
            for fk in FIELDS
        ]
    )
    result = await generate_candidates(
        conn=fake_conn,
        llm_client=client,
        model="qwen/qwen3.8-27b",
        field_keys=FIELDS,
        thoughts=graph_thoughts(),
        edges=graph_edges(),
        n_candidates=3,
        graph_context_enabled=True,
        context_tokens=300,
        temperature=0.0,
        repair_max_attempts=2,
        timeout_s=5.0,
    )
    cands = result.candidates_by_field["chief_complaint"]
    assert [c.variant for c in cands] == ["conservative", "inclusive", "negative_aware"]
    assert [c.index for c in cands] == [0, 1, 2]
    for c in cands:
        assert list(c.source_thought_ids) == ["t1", "t2"]
        assert c.generated_by_model == "qwen/qwen3.8-27b"
        assert c.is_refinement is False


async def test_list_fields_keep_items_and_scalars_keep_text(fake_conn) -> None:
    """A list field's `text` is the joined items (so the scorer has something
    to embed) while `items` stays the authoritative structure distillation
    reads. Losing either breaks a different downstream stage."""
    client = CassetteLLMClient(
        turns=[
            CassetteTurn(
                content=payload(
                    {"chief_complaint": field_payload(value="chest pain", ids=["t1"])}
                )
            ),
            CassetteTurn(
                content=payload(
                    {
                        "past_medical_history": field_payload(
                            items=["diabetes", "hypertension"], ids=["t4"]
                        )
                    }
                )
            ),
            CassetteTurn(content=payload({"allergies": field_payload()})),
        ]
    )
    result = await generate_candidates(
        conn=fake_conn,
        llm_client=client,
        model="m",
        field_keys=FIELDS,
        thoughts=graph_thoughts(),
        edges=graph_edges(),
        n_candidates=1,
        graph_context_enabled=True,
        context_tokens=300,
        temperature=0.0,
        repair_max_attempts=2,
        timeout_s=5.0,
    )
    pmh = result.candidates_by_field["past_medical_history"][0]
    assert list(pmh.items) == ["diabetes", "hypertension"]
    assert pmh.text == "diabetes, hypertension"
    cc = result.candidates_by_field["chief_complaint"][0]
    assert cc.text == "chest pain" and not cc.items
    # An empty field stays empty and cites nothing.
    allergies = result.candidates_by_field["allergies"][0]
    assert allergies.text == "" and not allergies.items
    assert not allergies.source_thought_ids


async def test_one_failing_variant_costs_a_candidate_not_the_consultation(
    fake_conn, caplog
) -> None:
    """A degraded pool of N-1 is a usable pool. Raising here would throw away
    a consultation's whole graph over one bad JSON response."""
    bad = CassetteTurn(content="not json at all")
    client = CassetteLLMClient(
        turns=[
            # variant 1 ("conservative"): every field succeeds.
            *[CassetteTurn(content=valid_response(fk)) for fk in FIELDS],
            # variant 2 ("inclusive"): its first field exhausts the repair
            # budget (repair_max_attempts=2 -> 3 attempts), which fails the
            # WHOLE variant immediately — its other two fields are never
            # attempted, so they need no scripted turns.
            bad,
            bad,
            bad,
            # variant 3 ("negative_aware"): every field succeeds.
            *[CassetteTurn(content=valid_response(fk)) for fk in FIELDS],
        ]
    )
    result = await generate_candidates(
        conn=fake_conn,
        llm_client=client,
        model="m",
        field_keys=FIELDS,
        thoughts=graph_thoughts(),
        edges=graph_edges(),
        n_candidates=3,
        graph_context_enabled=True,
        context_tokens=300,
        temperature=0.0,
        repair_max_attempts=2,
        timeout_s=5.0,
    )
    assert result.failed_variants == ["inclusive"]
    assert len(result.candidates_by_field["chief_complaint"]) == 2
    assert result.repair_attempts >= 2


async def test_repair_addendum_is_appended_and_the_retry_succeeds(fake_conn) -> None:
    """The repair loop must re-ask with the validation error attached, not
    resend the identical prompt — an identical prompt would also be a cache
    hit in production and return the same bad answer forever."""
    client = CassetteLLMClient(
        turns=[
            CassetteTurn(content="{ broken"),
            CassetteTurn(content=valid_response("chief_complaint")),
            CassetteTurn(content=valid_response("past_medical_history")),
            CassetteTurn(content=valid_response("allergies")),
        ]
    )
    result = await generate_candidates(
        conn=fake_conn,
        llm_client=client,
        model="m",
        field_keys=FIELDS,
        thoughts=graph_thoughts(),
        edges=graph_edges(),
        n_candidates=1,
        graph_context_enabled=True,
        context_tokens=300,
        temperature=0.0,
        repair_max_attempts=2,
        timeout_s=5.0,
    )
    assert result.failed_variants == []
    assert result.repair_attempts == 1
    first, second = client.calls[0]["user_prompt"], client.calls[1]["user_prompt"]
    assert second != first
    assert second.startswith(first)


async def test_a_candidate_citing_an_unknown_thought_is_rejected(fake_conn) -> None:
    """Semantic validation, not just schema: an invented thought id is an
    uncheckable evidence chain, which §3 scores as hallucination."""
    client = CassetteLLMClient(
        turns=[
            CassetteTurn(content=valid_response("chief_complaint", ids=["t-does-not-exist"]))
        ]
        * 3
    )
    with pytest.raises(FatalError) as exc:
        await generate_candidates(
            conn=fake_conn,
            llm_client=client,
            model="m",
            field_keys=FIELDS,
            thoughts=graph_thoughts(),
            edges=graph_edges(),
            n_candidates=1,
            graph_context_enabled=True,
            context_tokens=300,
            temperature=0.0,
            repair_max_attempts=2,
            timeout_s=5.0,
        )
    assert exc.value.code == "GENERATION_FAILED"


async def test_every_variant_failing_is_fatal(fake_conn) -> None:
    """Nothing to score and nothing to refine is not a degraded run, it is a
    failed one — and it must not reach distillation and emit an empty note."""
    client = CassetteLLMClient(turns=[CassetteTurn(content="nope")] * 12)
    with pytest.raises(FatalError) as exc:
        await generate_candidates(
            conn=fake_conn,
            llm_client=client,
            model="m",
            field_keys=FIELDS,
            thoughts=graph_thoughts(),
            edges=graph_edges(),
            n_candidates=3,
            graph_context_enabled=True,
            context_tokens=300,
            temperature=0.0,
            repair_max_attempts=2,
            timeout_s=5.0,
        )
    assert exc.value.code == "GENERATION_FAILED"
    assert "nothing to score or refine" in str(exc.value)


async def test_generation_context_budget_trims_expansion_but_never_seeds(fake_conn) -> None:
    """The narrower generation budget (300) versus retrieval's own (900) is
    what keeps a batched 8-field call inside §8's estimate — a regression to
    the retrieval default would roughly triple this stage's cost silently.

    Two halves, because the policy has two halves. Expanded (non-seed)
    thoughts are trimmed to fit. Seeds are not: when the seeds alone overflow,
    retrieval reports `budget_exceeded` rather than dropping evidence the
    field is *about*, and generation inherits that choice rather than
    re-truncating.
    """
    assert generation.DEFAULT_GENERATION_CONTEXT_TOKENS == 300

    # One seed, a long chain of non-seed neighbours: the budget bites on the
    # expansion, which is exactly what it is for.
    chain = [thought("t1", 1, "chest pain for two days", ["chest pain"])]
    chain += [
        thought(
            f"n{i}",
            i + 1,
            f"examination detail number {i} recorded at some length",
            [f"e{i}"],
            category=Cat.THOUGHT_CATEGORY_EXAMINATION,
        )
        for i in range(1, 20)
    ]
    chain_edges = [edge("t1", f"n{i}", EdgeT.EDGE_TYPE_ELABORATION, 0.9) for i in range(1, 20)]
    tight = build_variant_contexts(
        variant=VARIANTS[1],
        field_keys=["chief_complaint"],
        thoughts=chain,
        edges=chain_edges,
        context_tokens=120,
    )
    cc = tight.contexts["chief_complaint"]
    assert cc.estimated_tokens <= 120
    assert cc.truncated > 0
    assert not cc.budget_exceeded
    assert "t1" in {t.id for t in cc.thoughts}, "the seed always survives"

    # All seeds, no room: over budget and flagged, not silently pruned.
    all_seeds = [
        thought(f"s{i}", i, f"symptom number {i} described at length", [f"e{i}"])
        for i in range(1, 30)
    ]
    over = build_variant_contexts(
        variant=VARIANTS[1],
        field_keys=["chief_complaint"],
        thoughts=all_seeds,
        edges=[],
        context_tokens=120,
    )
    over_cc = over.contexts["chief_complaint"]
    assert over_cc.budget_exceeded
    assert over_cc.estimated_tokens > 120
    assert len(over_cc.thoughts) == len(all_seeds)
