"""GoT-HCS Module 2 — typed edge assembly.

Covers the rule layer (temporal edges from turn order, free in dialogue), the
LLM layer (the five semantic types), the heuristic weight that substitutes
for learned attention, and the interaction between the two layers.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from fixtures import transcripts

from coda.v1 import thought_pb2
from coda_worker_sdk.errors import FatalError
from nlp_service.graph import edges as edges_mod
from nlp_service.graph.edges import (
    EDGE_TYPE_PRIOR,
    assemble_edges,
    edge_weight,
    entity_overlap,
    temporal_edges,
)
from nlp_service.graph.thoughts import construct_thoughts
from nlp_service.llm.cassette import CassetteLLMClient, CassetteTurn

CASSETTES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "cassettes"

EdgeT = thought_pb2.EdgeType
Pred = thought_pb2.PredictedBy
Cat = thought_pb2.ThoughtCategory
Pol = thought_pb2.Polarity


def full_cassette(name: str) -> CassetteLLMClient:
    data = json.loads((CASSETTES / f"{name}.json").read_text())
    return CassetteLLMClient(turns=[CassetteTurn(**t) for t in data])


async def build_graph(name: str, factory, conn):  # type: ignore[no-untyped-def]
    """Construction then assembly, sharing one cassette so the call order the
    code issues is the order the fixture scripts."""
    transcript = factory()
    client = full_cassette(name)
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
    return transcript, construction.thoughts, assembly


def make_thought(tid: str, turn: int, entities: list[str], **kw) -> thought_pb2.Thought:  # type: ignore[no-untyped-def]
    return thought_pb2.Thought(
        id=tid,
        consultation_id="c",
        run_config_id="rc",
        turn_id=f"uuid-{turn}",
        turn_index=turn,
        text=kw.get("text", tid),
        entities=[thought_pb2.LinkedEntity(text=e) for e in entities],
        category=kw.get("category", Cat.THOUGHT_CATEGORY_SYMPTOM),
        polarity=kw.get("polarity", Pol.POLARITY_ASSERTED),
        confidence=1.0,
        char_start=kw.get("char_start", 0),
        char_end=kw.get("char_end", 10),
    )


# --------------------------------------------------------------------------
# The rule layer: temporal edges are free in dialogue
# --------------------------------------------------------------------------


def test_temporal_edges_chain_every_thought_in_turn_order() -> None:
    """claude_context.md §6 commits to exploiting turn order as temporal
    order — the structural advantage dialogue has over prose EHR text. The
    chain must be a single path so the graph is connected: retrieval falling
    back on an empty neighbourhood when the LLM layer predicts nothing is
    exactly what connectedness prevents.
    """
    thoughts = [
        make_thought("t1", 0, ["a"], char_start=0),
        make_thought("t2", 0, ["b"], char_start=20),  # same turn, later span
        make_thought("t3", 4, ["c"]),
        make_thought("t4", 9, ["d"]),
    ]
    edges = temporal_edges(thoughts)

    assert [(e.src_thought_id, e.dst_thought_id) for e in edges] == [
        ("t1", "t2"),
        ("t2", "t3"),
        ("t3", "t4"),
    ]
    assert all(e.edge_type == EdgeT.EDGE_TYPE_TEMPORAL for e in edges)
    assert all(e.predicted_by == Pred.PREDICTED_BY_RULE for e in edges)


def test_temporal_edges_are_direction_bearing() -> None:
    """A dialogue has an arrow of time: "the pain started, then I took
    ibuprofen" is not the same claim reversed."""
    thoughts = [make_thought("t1", 0, ["a"]), make_thought("t2", 1, ["b"])]
    edges = temporal_edges(thoughts)
    assert (edges[0].src_thought_id, edges[0].dst_thought_id) == ("t1", "t2")


def test_temporal_edges_cost_no_llm_calls(fake_conn: object) -> None:
    """The claim being protected: the temporal backbone is obtained for zero
    tokens. `temporal_edges` takes no client and no connection at all, which
    is the strongest form this assertion can take."""
    import inspect

    params = set(inspect.signature(temporal_edges).parameters)
    assert params == {"thoughts"}


async def test_model_predicted_temporal_edges_are_rejected(fake_conn: object) -> None:
    """Accepting a model-authored temporal edge would blend a rule signal
    and a model signal into one field and make "temporal edges are free"
    unfalsifiable. The schema rejects it; the repair addendum says why.
    """
    thoughts = [make_thought("t1", 0, ["cough"]), make_thought("t2", 1, ["cough"])]
    bad = {
        "edges": [
            {
                "src_thought_id": "t1",
                "dst_thought_id": "t2",
                "edge_type": "temporal",
                "confidence": 0.9,
                "rationale": "consecutive turns",
            }
        ]
    }
    client = CassetteLLMClient(
        turns=[CassetteTurn(content=json.dumps(bad)), CassetteTurn(content=json.dumps(bad))]
    )
    with pytest.raises(FatalError) as exc:
        await edges_mod.predict_llm_edges(
            conn=fake_conn,  # type: ignore[arg-type]
            llm_client=client,
            model="qwen/qwen3.6-27b",
            thoughts=thoughts,
            consultation_id="c",
            run_config_id="rc",
            repair_max_attempts=1,
            timeout_s=5.0,
        )
    assert exc.value.code == "EDGE_PREDICTION_INVALID"
    assert "temporal" in str(client.calls[1]["user_prompt"])


# --------------------------------------------------------------------------
# The heuristic weight — the retained piece of the replaced GAT
# --------------------------------------------------------------------------


def test_entity_overlap_is_jaccard_and_case_insensitive() -> None:
    a = make_thought("a", 0, ["Cough", "fever"])
    b = make_thought("b", 1, ["cough", "chest"])
    assert entity_overlap(a, b) == pytest.approx(1 / 3)
    assert entity_overlap(a, make_thought("c", 2, [])) == 0.0


def test_weight_is_overlap_times_prior_times_model_confidence() -> None:
    """claude_context.md §6's `entity_overlap × edge_type_prior`, with LLM
    confidence scaling the model-predicted half."""
    a = make_thought("a", 0, ["cough", "fever"])
    b = make_thought("b", 1, ["cough", "fever"])  # overlap 1.0
    assert edge_weight(a, b, EdgeT.EDGE_TYPE_CAUSAL) == pytest.approx(
        EDGE_TYPE_PRIOR[EdgeT.EDGE_TYPE_CAUSAL]
    )
    assert edge_weight(a, b, EdgeT.EDGE_TYPE_CAUSAL, model_confidence=0.5) == pytest.approx(
        EDGE_TYPE_PRIOR[EdgeT.EDGE_TYPE_CAUSAL] * 0.5
    )


def test_paraphrase_across_turns_is_not_zeroed_by_the_overlap_floor() -> None:
    """Entity overlap is a lexical proxy that fails on speaker paraphrase —
    ubiquitous in dialogue, rare in the prose the heuristic came from.
    Without the floor a correct negation edge between "chest pain" and
    "the discomfort" would weigh 0.0 and be dropped entirely.
    """
    a = make_thought("a", 3, ["chest pain"])
    b = make_thought("b", 9, ["discomfort"])
    assert entity_overlap(a, b) == 0.0
    assert edge_weight(a, b, EdgeT.EDGE_TYPE_NEGATION) > edges_mod.MIN_EDGE_WEIGHT


def test_negation_carries_the_highest_prior() -> None:
    """Dropping a negation from context inverts clinical meaning, which is
    strictly worse than omitting a fact — so it outranks every other type."""
    assert EDGE_TYPE_PRIOR[EdgeT.EDGE_TYPE_NEGATION] == max(EDGE_TYPE_PRIOR.values())
    assert EDGE_TYPE_PRIOR[EdgeT.EDGE_TYPE_TEMPORAL] == min(EDGE_TYPE_PRIOR.values())


# --------------------------------------------------------------------------
# The two layers together, on the fixtures
# --------------------------------------------------------------------------


async def test_negation_case_produces_a_negation_edge_between_the_two_thoughts(
    fake_conn: object,
) -> None:
    """The turn-3/turn-9 pair must be linked, not merely both present. An
    unlinked negation means retrieval can pull the assertion into a prompt
    without its retraction.
    """
    _, thoughts, assembly = await build_graph(
        "negation", transcripts.negation_transcript, fake_conn
    )
    by_id = {t.id: t for t in thoughts}
    negations = [e for e in assembly.edges if e.edge_type == EdgeT.EDGE_TYPE_NEGATION]
    assert len(negations) == 1

    edge = negations[0]
    src, dst = by_id[edge.src_thought_id], by_id[edge.dst_thought_id]
    assert (src.turn_index, dst.turn_index) == (3, 9)
    assert src.polarity == Pol.POLARITY_ASSERTED
    assert dst.polarity == Pol.POLARITY_NEGATED
    assert edge.predicted_by == Pred.PREDICTED_BY_LLM
    assert edge.rationale


async def test_cross_turn_symptom_is_reassembled_by_elaboration_edges(
    fake_conn: object,
) -> None:
    """The three cough fragments (turns 1, 5, 9) must end up mutually
    reachable through elaboration edges — that reassembly is what the graph
    contributes over reading turns in isolation.
    """
    _, thoughts, assembly = await build_graph(
        "cross_turn_symptom", transcripts.cross_turn_symptom_transcript, fake_conn
    )
    by_id = {t.id: t for t in thoughts}
    cough_ids = {t.id for t in thoughts if any(e.text.lower() == "cough" for e in t.entities)}
    assert {by_id[i].turn_index for i in cough_ids} == {1, 5, 9}

    elaborations = [
        (e.src_thought_id, e.dst_thought_id)
        for e in assembly.edges
        if e.edge_type == EdgeT.EDGE_TYPE_ELABORATION
    ]
    linked = {a for a, b in elaborations} | {b for a, b in elaborations}
    assert cough_ids <= linked


async def test_llm_edge_supersedes_the_temporal_edge_on_the_same_pair(
    fake_conn: object,
) -> None:
    """A pair that is both adjacent and causally related is more usefully
    described as causal, and keeping both would double-count that pair's
    weight during retrieval.
    """
    thoughts = [make_thought("t1", 0, ["pain"]), make_thought("t2", 1, ["pain"])]
    predicted = {
        "edges": [
            {
                "src_thought_id": "t1",
                "dst_thought_id": "t2",
                "edge_type": "causal",
                "confidence": 0.9,
                "rationale": "trigger",
            }
        ]
    }
    client = CassetteLLMClient(turns=[CassetteTurn(content=json.dumps(predicted))])
    assembly = await assemble_edges(
        conn=fake_conn,  # type: ignore[arg-type]
        llm_client=client,
        model="qwen/qwen3.6-27b",
        thoughts=thoughts,
        consultation_id="c",
        run_config_id="rc",
        repair_max_attempts=1,
        timeout_s=5.0,
    )
    pair_edges = [e for e in assembly.edges if (e.src_thought_id, e.dst_thought_id) == ("t1", "t2")]
    assert len(pair_edges) == 1
    assert pair_edges[0].edge_type == EdgeT.EDGE_TYPE_CAUSAL
    assert assembly.rule_edge_count == 0
    assert assembly.llm_edge_count == 1


async def test_single_thought_graph_skips_the_edge_call(fake_conn: object) -> None:
    """A model asked to relate one item to itself will invent a self-edge to
    fill the schema, so the call is not made at all."""
    client = CassetteLLMClient(turns=[])
    assembly = await assemble_edges(
        conn=fake_conn,  # type: ignore[arg-type]
        llm_client=client,
        model="qwen/qwen3.6-27b",
        thoughts=[make_thought("t1", 0, ["cough"])],
        consultation_id="c",
        run_config_id="rc",
        repair_max_attempts=1,
        timeout_s=5.0,
    )
    assert assembly.edges == []
    assert assembly.llm_calls == 0
    assert client.calls == []


@pytest.mark.parametrize(
    ("name", "factory"),
    [
        ("negation", transcripts.negation_transcript),
        ("interruption", transcripts.interruption_transcript),
        ("disfluency", transcripts.disfluency_transcript),
        ("cross_turn_symptom", transcripts.cross_turn_symptom_transcript),
    ],
)
async def test_graph_is_well_formed_and_connected(
    name: str, factory: object, fake_conn: object
) -> None:
    _, thoughts, assembly = await build_graph(name, factory, fake_conn)
    ids = {t.id for t in thoughts}

    for edge in assembly.edges:
        assert edge.src_thought_id in ids
        assert edge.dst_thought_id in ids
        assert edge.src_thought_id != edge.dst_thought_id
        assert edge.weight >= edges_mod.MIN_EDGE_WEIGHT
        assert edge.consultation_id == "c-fixture"

    # Undirected connectivity, guaranteed by the temporal backbone.
    adj: dict[str, set[str]] = {i: set() for i in ids}
    for edge in assembly.edges:
        adj[edge.src_thought_id].add(edge.dst_thought_id)
        adj[edge.dst_thought_id].add(edge.src_thought_id)
    seen = {next(iter(ids))}
    stack = list(seen)
    while stack:
        for nxt in adj[stack.pop()]:
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    assert seen == ids, "the temporal backbone must leave the graph connected"
