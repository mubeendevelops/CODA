"""Graph-structured context retrieval — the substitute for the trained GAT.

The properties under test are the ones the substitution actually rests on:
structure (not just topic match) decides membership; edge weight decides
strength; a negation can never be budget-truncated away from what it negates;
and the whole thing costs far fewer tokens than the transcript it replaces
(claude_context.md §6's "GAT" row and §8's mitigation #3).
"""

from __future__ import annotations

import json
import pathlib

import pytest
from fixtures import transcripts

from coda.v1 import thought_pb2
from nlp_service.graph.edges import assemble_edges
from nlp_service.graph.retrieval import (
    FIELD_SEED_CATEGORIES,
    RetrievalPolicy,
    estimate_tokens,
    render_context,
    retrieve_all_fields,
    retrieve_field_context,
)
from nlp_service.graph.thoughts import construct_thoughts
from nlp_service.llm.cassette import CassetteLLMClient, CassetteTurn

CASSETTES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "cassettes"

EdgeT = thought_pb2.EdgeType
Cat = thought_pb2.ThoughtCategory
Pol = thought_pb2.Polarity


async def build_graph(name: str, factory, conn):  # type: ignore[no-untyped-def]
    transcript = factory()
    data = json.loads((CASSETTES / f"{name}.json").read_text())
    client = CassetteLLMClient(turns=[CassetteTurn(**t) for t in data])
    construction = await construct_thoughts(
        conn=conn,
        llm_client=client,
        model="qwen/qwen3.8-27b",
        transcript=transcript,
        consultation_id="c-fixture",
        run_config_id="rc-fixture",
        turn_id_by_index={t.turn_index: f"uuid-turn-{t.turn_index}" for t in transcript.turns},
        repair_max_attempts=2,
        timeout_s=5.0,
    )
    assembly = await assemble_edges(
        conn=conn,
        llm_client=client,
        model="qwen/qwen3.6-27b",
        thoughts=construction.thoughts,
        consultation_id="c-fixture",
        run_config_id="rc-fixture",
        repair_max_attempts=2,
        timeout_s=5.0,
    )
    return transcript, construction.thoughts, assembly.edges


def thought(tid: str, turn: int, entities: list[str], category, **kw):  # type: ignore[no-untyped-def]
    return thought_pb2.Thought(
        id=tid,
        consultation_id="c",
        run_config_id="rc",
        turn_id=f"uuid-{turn}",
        turn_index=turn,
        text=kw.get("text", tid),
        entities=[thought_pb2.LinkedEntity(text=e) for e in entities],
        category=category,
        polarity=kw.get("polarity", Pol.POLARITY_ASSERTED),
        confidence=1.0,
        char_start=kw.get("char_start", 0),
        char_end=kw.get("char_end", 10),
    )


def edge(src: str, dst: str, etype, weight: float):  # type: ignore[no-untyped-def]
    return thought_pb2.ThoughtEdge(
        consultation_id="c",
        run_config_id="rc",
        src_thought_id=src,
        dst_thought_id=dst,
        edge_type=etype,
        weight=weight,
        predicted_by=thought_pb2.PredictedBy.PREDICTED_BY_LLM,
    )


# --------------------------------------------------------------------------
# Seeding and traversal
# --------------------------------------------------------------------------


def test_seeds_come_from_the_field_category_mapping() -> None:
    thoughts = [
        thought("t1", 0, ["cough"], Cat.THOUGHT_CATEGORY_SYMPTOM),
        thought("t2", 1, ["aspirin"], Cat.THOUGHT_CATEGORY_MEDICATION),
        thought("t3", 2, ["x-ray"], Cat.THOUGHT_CATEGORY_INVESTIGATION),
    ]
    ctx = retrieve_field_context(field_key="investigations_advised", thoughts=thoughts, edges=[])
    assert ctx.seed_ids == ("t3",)
    assert [t.id for t in ctx.thoughts] == ["t3"]


def test_traversal_pulls_in_structurally_linked_thoughts_of_other_categories() -> None:
    """The point of retrieving over the graph rather than filtering by
    category: an examination finding that supports a diagnosis belongs in
    the diagnosis field's context even though its category does not match.
    """
    thoughts = [
        thought("t1", 0, ["strep"], Cat.THOUGHT_CATEGORY_DIAGNOSIS),
        thought("t2", 1, ["tonsils", "strep"], Cat.THOUGHT_CATEGORY_EXAMINATION),
        thought("t3", 2, ["ibuprofen"], Cat.THOUGHT_CATEGORY_MEDICATION),
    ]
    edges = [edge("t2", "t1", EdgeT.EDGE_TYPE_LOGICAL, 0.8)]
    ctx = retrieve_field_context(field_key="provisional_diagnosis", thoughts=thoughts, edges=edges)
    ids = {t.id for t in ctx.thoughts}
    assert "t2" in ids, "the supporting examination finding must be reachable"
    assert "t3" not in ids, "an unlinked medication must not be"


def test_depth_is_capped() -> None:
    thoughts = [
        thought("t1", 0, ["a"], Cat.THOUGHT_CATEGORY_DIAGNOSIS),
        thought("t2", 1, ["a"], Cat.THOUGHT_CATEGORY_EXAMINATION),
        thought("t3", 2, ["a"], Cat.THOUGHT_CATEGORY_EXAMINATION),
        thought("t4", 3, ["a"], Cat.THOUGHT_CATEGORY_EXAMINATION),
    ]
    edges = [
        edge("t1", "t2", EdgeT.EDGE_TYPE_LOGICAL, 0.9),
        edge("t2", "t3", EdgeT.EDGE_TYPE_LOGICAL, 0.9),
        edge("t3", "t4", EdgeT.EDGE_TYPE_LOGICAL, 0.9),
    ]
    at_1 = retrieve_field_context(
        field_key="provisional_diagnosis",
        thoughts=thoughts,
        edges=edges,
        policy=RetrievalPolicy(max_depth=1),
    )
    at_2 = retrieve_field_context(
        field_key="provisional_diagnosis",
        thoughts=thoughts,
        edges=edges,
        policy=RetrievalPolicy(max_depth=2),
    )
    assert {t.id for t in at_1.thoughts} == {"t1", "t2"}
    assert {t.id for t in at_2.thoughts} == {"t1", "t2", "t3"}


def test_edge_weight_decides_strength_not_only_membership() -> None:
    """The retained half of the replaced GAT: a strongly-linked neighbour
    must outrank a weakly-linked one, so that under budget pressure the
    weaker one is what gets cut.
    """
    thoughts = [
        thought("t1", 0, ["a"], Cat.THOUGHT_CATEGORY_DIAGNOSIS),
        thought("strong", 1, ["a"], Cat.THOUGHT_CATEGORY_EXAMINATION),
        thought("weak", 2, ["a"], Cat.THOUGHT_CATEGORY_EXAMINATION),
    ]
    edges = [
        edge("t1", "strong", EdgeT.EDGE_TYPE_LOGICAL, 0.9),
        edge("t1", "weak", EdgeT.EDGE_TYPE_LOGICAL, 0.1),
    ]
    ctx = retrieve_field_context(field_key="provisional_diagnosis", thoughts=thoughts, edges=edges)
    assert ctx.priority["strong"] > ctx.priority["weak"]


def test_backward_traversal_is_decayed() -> None:
    """A thought that elaborates a seed is more relevant to that seed than
    an arbitrary thought the seed elaborates."""
    thoughts = [
        thought("seed", 0, ["a"], Cat.THOUGHT_CATEGORY_DIAGNOSIS),
        thought("fwd", 1, ["a"], Cat.THOUGHT_CATEGORY_EXAMINATION),
        thought("back", 2, ["a"], Cat.THOUGHT_CATEGORY_EXAMINATION),
    ]
    edges = [
        edge("seed", "fwd", EdgeT.EDGE_TYPE_LOGICAL, 0.8),
        edge("back", "seed", EdgeT.EDGE_TYPE_LOGICAL, 0.8),
    ]
    ctx = retrieve_field_context(field_key="provisional_diagnosis", thoughts=thoughts, edges=edges)
    assert ctx.priority["fwd"] > ctx.priority["back"]


# --------------------------------------------------------------------------
# The guarantee that matters: negation is never truncated away
# --------------------------------------------------------------------------


def test_negation_is_followed_past_the_depth_cap() -> None:
    """A negation five hops from a seed is still reached the moment its
    target is selected — otherwise the prompt gets an asserted symptom with
    no trace of its retraction, which is not a smaller context but a wrong
    one.
    """
    thoughts = [
        thought("t1", 0, ["a"], Cat.THOUGHT_CATEGORY_ALLERGY),
        thought("t2", 1, ["a"], Cat.THOUGHT_CATEGORY_OTHER),
        thought("t3", 2, ["a"], Cat.THOUGHT_CATEGORY_OTHER),
        thought("t4", 3, ["a"], Cat.THOUGHT_CATEGORY_OTHER),
        thought("denial", 9, ["a"], Cat.THOUGHT_CATEGORY_OTHER, polarity=Pol.POLARITY_NEGATED),
    ]
    edges = [
        edge("t1", "t2", EdgeT.EDGE_TYPE_TEMPORAL, 0.3),
        edge("t2", "t3", EdgeT.EDGE_TYPE_TEMPORAL, 0.3),
        edge("t3", "t4", EdgeT.EDGE_TYPE_TEMPORAL, 0.3),
        edge("t1", "denial", EdgeT.EDGE_TYPE_NEGATION, 1.0),
    ]
    ctx = retrieve_field_context(
        field_key="allergies", thoughts=thoughts, edges=edges, policy=RetrievalPolicy(max_depth=1)
    )
    assert "denial" in {t.id for t in ctx.thoughts}
    assert "denial" in ctx.guaranteed_ids


def test_negation_survives_a_budget_too_small_to_hold_it() -> None:
    """Budget is enforced after the guaranteed set, never over it. Dropping
    a negation to hit a token number is a worse failure than a larger
    prompt, so the overflow is reported instead.
    """
    thoughts = [
        thought("t1", 0, ["a"], Cat.THOUGHT_CATEGORY_ALLERGY, text="x" * 400),
        thought(
            "denial",
            9,
            ["a"],
            Cat.THOUGHT_CATEGORY_OTHER,
            text="y" * 400,
            polarity=Pol.POLARITY_NEGATED,
        ),
        thought("filler", 5, ["a"], Cat.THOUGHT_CATEGORY_OTHER, text="z" * 400),
    ]
    edges = [
        edge("t1", "denial", EdgeT.EDGE_TYPE_NEGATION, 1.0),
        edge("t1", "filler", EdgeT.EDGE_TYPE_ELABORATION, 0.9),
    ]
    ctx = retrieve_field_context(
        field_key="allergies",
        thoughts=thoughts,
        edges=edges,
        policy=RetrievalPolicy(token_budget=50),
    )
    ids = {t.id for t in ctx.thoughts}
    assert {"t1", "denial"} <= ids
    assert "filler" not in ids
    assert ctx.budget_exceeded is True
    assert ctx.truncated == 1


async def test_negation_case_context_carries_both_polarities(fake_conn: object) -> None:
    """End to end on the fixture: retrieving the allergies field must return
    both the patient's claim and the doctor's correction, with polarity
    visible in the rendered prompt text.
    """
    _, thoughts, edges = await build_graph("negation", transcripts.negation_transcript, fake_conn)
    ctx = retrieve_field_context(field_key="allergies", thoughts=thoughts, edges=edges)

    polarities = {
        t.polarity for t in ctx.thoughts if any("penicillin" in e.text for e in t.entities)
    }
    assert polarities == {Pol.POLARITY_ASSERTED, Pol.POLARITY_NEGATED}

    rendered = render_context(ctx)
    assert "negated" in rendered
    assert "asserted" in rendered


async def test_cross_turn_symptom_context_gathers_all_three_fragments(
    fake_conn: object,
) -> None:
    """`hopi` retrieval must return the cough's turn-1, turn-5 and turn-9
    fragments together, which is what reading turns in isolation cannot do.
    """
    _, thoughts, edges = await build_graph(
        "cross_turn_symptom", transcripts.cross_turn_symptom_transcript, fake_conn
    )
    ctx = retrieve_field_context(field_key="hopi", thoughts=thoughts, edges=edges)
    cough_turns = {
        t.turn_index for t in ctx.thoughts if any(e.text.lower() == "cough" for e in t.entities)
    }
    assert cough_turns == {1, 5, 9}


# --------------------------------------------------------------------------
# Budget, ordering, and the token saving the mechanism exists to produce
# --------------------------------------------------------------------------


def test_context_is_emitted_in_reading_order_not_priority_order() -> None:
    """A generation model reads a consultation better chronologically, and
    the chronology is free information we already have."""
    thoughts = [
        thought("late", 9, ["a"], Cat.THOUGHT_CATEGORY_SYMPTOM),
        thought("early", 1, ["a"], Cat.THOUGHT_CATEGORY_SYMPTOM),
        thought("mid", 5, ["a"], Cat.THOUGHT_CATEGORY_SYMPTOM),
    ]
    ctx = retrieve_field_context(field_key="hopi", thoughts=thoughts, edges=[])
    assert [t.turn_index for t in ctx.thoughts] == [1, 5, 9]


def test_unprotected_thoughts_are_truncated_under_budget() -> None:
    thoughts = [thought("seed", 0, ["a"], Cat.THOUGHT_CATEGORY_SYMPTOM)] + [
        thought(f"n{i}", i, ["a"], Cat.THOUGHT_CATEGORY_OTHER, text="w" * 200) for i in range(1, 8)
    ]
    edges = [edge("seed", f"n{i}", EdgeT.EDGE_TYPE_ELABORATION, 0.9) for i in range(1, 8)]
    ctx = retrieve_field_context(
        field_key="hopi", thoughts=thoughts, edges=edges, policy=RetrievalPolicy(token_budget=200)
    )
    assert ctx.truncated > 0
    assert ctx.budget_exceeded is False
    assert "seed" in {t.id for t in ctx.thoughts}


def test_max_thoughts_ceiling_is_independent_of_the_token_budget() -> None:
    thoughts = [thought("seed", 0, ["a"], Cat.THOUGHT_CATEGORY_SYMPTOM)] + [
        thought(f"n{i}", i, ["a"], Cat.THOUGHT_CATEGORY_OTHER, text="ok") for i in range(1, 30)
    ]
    edges = [edge("seed", f"n{i}", EdgeT.EDGE_TYPE_ELABORATION, 0.9) for i in range(1, 30)]
    ctx = retrieve_field_context(
        field_key="hopi",
        thoughts=thoughts,
        edges=edges,
        policy=RetrievalPolicy(token_budget=100_000, max_thoughts=5),
    )
    assert len(ctx.thoughts) == 5


@pytest.mark.parametrize(
    ("name", "factory", "max_ratio"),
    [
        # Measured, not aspirational. Ratio = (8 field contexts) / (8 x full
        # transcript) — the second number being the actual alternative, one
        # field-generation prompt per clinical field each carrying the whole
        # conversation. Bounds are the measured value plus headroom.
        ("negation", transcripts.negation_transcript, 1.10),  # measured 1.03
        ("interruption", transcripts.interruption_transcript, 0.95),  # measured 0.86
        ("disfluency", transcripts.disfluency_transcript, 0.80),  # measured 0.71
        ("cross_turn_symptom", transcripts.cross_turn_symptom_transcript, 0.95),  # 0.89
    ],
)
async def test_graph_context_token_cost_against_the_transcript_alternative(
    name: str, factory: object, max_ratio: float, fake_conn: object
) -> None:
    """Pins the token cost of graph context on each fixture, honestly.

    Note the negation fixture's bound: **above 1.0**. On that transcript
    graph context is measurably *more* expensive than sending the transcript
    eight times (1.03x). That is not a bug and it is not tuned away here —
    it is what the mechanism costs on an 11-turn consultation dense with
    multi-entity thoughts, where per-thought annotation (speaker, category,
    polarity, entities) outweighs the short raw turns it replaces.

    claude_context.md §8 claims graph-retrieved context as the pipeline's
    largest token saving. That claim is about real 10-minute consultations,
    where the transcript far outgrows the per-field budget — see
    `test_per_field_context_is_bounded_while_the_transcript_is_not` for the
    scaling that makes it true. It does **not** hold uniformly at fixture
    scale, and a short-transcript eval would understate the mechanism. These
    bounds exist so that stays visible instead of surfacing as a surprise in
    the Phase 10 numbers.
    """
    transcript, thoughts, edges = await build_graph(name, factory, fake_conn)
    full = estimate_tokens(
        "\n".join(f"{t.turn_index}: {t.text_redacted}" for t in transcript.turns)
    )
    contexts = retrieve_all_fields(thoughts=thoughts, edges=edges)
    graph_total = sum(c.estimated_tokens for c in contexts.values())
    assert graph_total / (full * 8) <= max_ratio


def test_per_field_context_is_bounded_while_the_transcript_is_not() -> None:
    """The property that makes the saving scale: a field's context saturates
    at the token budget no matter how long the consultation gets, while a
    transcript grows without bound. This is what turns a modest saving on a
    short consultation into a large one on a real 10-minute OPD dialogue.

    Measured on the synthetic graph below (~1.4 thoughts/turn, matching the
    density observed across the four fixtures), against the 8 × transcript
    alternative:

        turns   transcript   8 x transcript   max field ctx   8 fields, graph
           10          253             2024             353              1764
           30          763             6104             885              5193
           60         1528            12224             900              7132
          120         3063            24504             895              7131

    Note the honest shape of this: the saving is real but modest on short
    consultations (13% at 10 turns), because per-thought annotation —
    speaker, category, polarity, entities — costs tokens the raw turn text
    does not. It becomes decisive only once the transcript outgrows the
    per-field budget, which is where real consultations sit (71% at 120
    turns). A short-transcript eval would understate the mechanism's value,
    and this test exists so that stays visible rather than being discovered
    as a surprise in the Phase 10 numbers.
    """

    def synthetic(n_turns: int):  # type: ignore[no-untyped-def]
        cats = [
            Cat.THOUGHT_CATEGORY_SYMPTOM,
            Cat.THOUGHT_CATEGORY_HISTORY,
            Cat.THOUGHT_CATEGORY_MEDICATION,
            Cat.THOUGHT_CATEGORY_EXAMINATION,
            Cat.THOUGHT_CATEGORY_DIAGNOSIS,
            Cat.THOUGHT_CATEGORY_INVESTIGATION,
            Cat.THOUGHT_CATEGORY_PLAN,
            Cat.THOUGHT_CATEGORY_OTHER,
        ]
        turn_text = (
            "This is a representative consultation turn of roughly average "
            "length for an OPD dialogue."
        )
        ths, egs = [], []
        for turn in range(n_turns):
            for _ in range(1 + (turn % 2)):
                ths.append(
                    thought(
                        f"t{len(ths) + 1}",
                        turn,
                        [f"e{turn % 7}"],
                        cats[turn % len(cats)],
                        text="Representative clinical assertion drawn from this turn",
                    )
                )
        for a, b in zip(ths, ths[1:], strict=False):
            egs.append(edge(a.id, b.id, EdgeT.EDGE_TYPE_TEMPORAL, 0.3))
        full = estimate_tokens("\n".join(f"{i}: doctor: {turn_text}" for i in range(n_turns)))
        ctxs = retrieve_all_fields(thoughts=ths, edges=egs)
        return (
            full,
            max(c.estimated_tokens for c in ctxs.values()),
            sum(c.estimated_tokens for c in ctxs.values()),
        )

    budget = RetrievalPolicy().token_budget
    for n_turns in (10, 30, 60, 120):
        full, max_field, graph_total = synthetic(n_turns)
        assert max_field <= budget, "a field's context must never exceed its budget"
        assert graph_total < full * 8, f"graph context must beat 8 x transcript at {n_turns} turns"

    # At 120 turns the per-field context is fully saturated and the
    # transcript is four times larger than it was at 30.
    full_120, max_120, total_120 = synthetic(120)
    full_30, _, total_30 = synthetic(30)
    assert full_120 > full_30 * 3
    assert total_120 < total_30 * 2, "graph cost must stay near-flat as the transcript grows"


async def test_every_clinical_field_retrieves_without_error(fake_conn: object) -> None:
    """One retrieval per field is what a generation pass batched across the
    8 fields needs; a field with no seeds returns empty rather than raising.
    """
    _, thoughts, edges = await build_graph(
        "interruption", transcripts.interruption_transcript, fake_conn
    )
    contexts = retrieve_all_fields(thoughts=thoughts, edges=edges)
    assert set(contexts) == set(FIELD_SEED_CATEGORIES)
    # This fixture has no medication or allergy thoughts at all.
    assert contexts["allergies"].thoughts == []
    assert contexts["hopi"].thoughts


def test_unknown_field_key_is_rejected() -> None:
    with pytest.raises(KeyError, match="unknown field_key"):
        retrieve_field_context(field_key="not_a_field", thoughts=[], edges=[])


def test_field_seed_categories_match_the_persisted_field_keys() -> None:
    """Retrieval keys must be the same 8-field keys `extractions` stores, or
    a per-field ablation comparison stops being a GROUP BY."""
    from nlp_service.db import FIELD_KEYS

    assert set(FIELD_SEED_CATEGORIES) == set(FIELD_KEYS)
