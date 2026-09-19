"""GoT-HCS Module 2 — thought-graph assembly with typed edges.

Two layers, deliberately separated:

**The rule layer** derives temporal edges from turn order. This is the
structural advantage conversational input has over the prose EHR text
GoT-HCS was designed for, and claude_context.md §6 commits to exploiting it:
in dialogue, turn order *is* temporal order, so the entire temporal backbone
of the graph is obtained for **zero tokens and zero model calls**. In prose
EHR text the same edges require either a trained predictor or an LLM call per
candidate pair. We spend nothing on them. The prompt (edge_prediction_*.md)
explicitly forbids the model from emitting temporal edges, and
graph/schema.py rejects them — mixing a rule signal and a model signal in one
edge type would make that claim unfalsifiable.

**The LLM layer** predicts the five relations turn order cannot give us:
causal, logical, negation, elaboration, and coreference. It runs on
`RunConfig.structural_model`, per claude_context.md §4's "structural calls —
edge typing" assignment, which also keeps it on a separate Groq quota bucket
from the base model doing thought construction and generation (§8).

**Edge weight** is `entity_overlap × edge_type_prior`, the heuristic
claude_context.md §6 retains in place of the trained GAT's learned attention.
It is what makes graph structure influence aggregation *strength* and not
merely membership. LLM-predicted edges additionally scale by the model's own
confidence, so a hedged edge pulls its neighbourhood in more weakly than a
certain one — the closest thing to a learned weight available without
training data.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import psycopg

from coda.v1 import thought_pb2
from coda_worker_sdk.errors import FatalError
from nlp_service import prompts
from nlp_service.graph import schema
from nlp_service.graph.thoughts import CATEGORY_NAME, POLARITY_NAME, SPEAKER_NAME
from nlp_service.llm.client import LLMClient
from nlp_service.llm.limits import OTPM_SAFE_MAX_TOKENS
from nlp_service.llm_cache import complete_cached

logger = logging.getLogger(__name__)

EdgeT = thought_pb2.EdgeType

_EDGE_TYPE_BY_NAME = {
    "temporal": EdgeT.EDGE_TYPE_TEMPORAL,
    "causal": EdgeT.EDGE_TYPE_CAUSAL,
    "logical": EdgeT.EDGE_TYPE_LOGICAL,
    "negation": EdgeT.EDGE_TYPE_NEGATION,
    "elaboration": EdgeT.EDGE_TYPE_ELABORATION,
    "coreference": EdgeT.EDGE_TYPE_COREFERENCE,
}
EDGE_TYPE_NAME = {v: k for k, v in _EDGE_TYPE_BY_NAME.items()}

EDGE_TYPE_PRIOR: dict[int, float] = {
    EdgeT.EDGE_TYPE_TEMPORAL: 0.3,
    EdgeT.EDGE_TYPE_CAUSAL: 0.9,
    EdgeT.EDGE_TYPE_LOGICAL: 0.85,
    EdgeT.EDGE_TYPE_NEGATION: 1.0,
    EdgeT.EDGE_TYPE_ELABORATION: 0.8,
    EdgeT.EDGE_TYPE_COREFERENCE: 0.7,
}
"""How much each relation type is worth when deciding what enters a
generation prompt. These are priors, not measurements — they are stated here
as one table so the report can name them and an ablation can vary them,
rather than having them scattered as literals.

Ordering rationale: NEGATION is 1.0 because dropping a negation from context
is the one failure that actively inverts clinical meaning ("no chest pain"
becoming "chest pain"), which is strictly worse than omitting a fact.
TEMPORAL is 0.3 — adjacency in a conversation is weak evidence of clinical
relatedness, since consecutive turns are frequently about unrelated things,
and every thought has temporal neighbours, so a higher prior would let the
cheap edges crowd out the informative ones.
"""

MIN_EDGE_WEIGHT = 0.05
"""Edges below this are dropped rather than stored. An edge with no entity
overlap and a weak prior contributes nothing to retrieval but still costs a
row, an index entry, and a line in every graph figure."""

## max_tokens history (2026-09-05): see nlp_service.llm.limits' docstring —
## first scaled up per-thought to stop hidden reasoning from starving the
## answer, then found that Groq's separate OTPM per-minute ceiling rejects a
## large max_tokens outright; `reasoning_effort: "none"` (client-level) fixed
## the original problem, so this uses OTPM_SAFE_MAX_TOKENS directly.


@dataclass(frozen=True, slots=True)
class EdgeAssemblyResult:
    edges: list[thought_pb2.ThoughtEdge]
    tokens_in: int = 0
    tokens_out: int = 0
    llm_calls: int = 0
    cache_hits: int = 0
    repair_attempts: int = 0
    rule_edge_count: int = 0
    llm_edge_count: int = 0


def entity_overlap(a: thought_pb2.Thought, b: thought_pb2.Thought) -> float:
    """Jaccard over the two thoughts' entity sets, case-folded.

    Jaccard rather than a raw count so a thought that happens to mention many
    entities does not dominate every edge it touches. Two thoughts sharing
    no entities score 0.0, which is why `_weight` floors the overlap term
    instead of multiplying straight through: a genuine causal or negation
    relation between thoughts that name their subject differently ("the pain"
    vs "chest discomfort") must not be zeroed out by a lexical miss.
    """
    ea = {e.text.strip().lower() for e in a.entities if e.text.strip()}
    eb = {e.text.strip().lower() for e in b.entities if e.text.strip()}
    if not ea or not eb:
        return 0.0
    union = ea | eb
    return len(ea & eb) / len(union)


_OVERLAP_FLOOR = 0.25
"""Floor on the entity-overlap term. Entity overlap is a lexical proxy for
semantic relatedness and it fails in exactly the case that matters most:
speaker paraphrase across turns, which is ubiquitous in dialogue and rare in
the prose EHR text the original heuristic came from. Without a floor, a
correctly-predicted negation edge between "I get chest pain" and "no, the
discomfort has stopped" would carry weight 0.0 and be dropped."""


def edge_weight(
    src: thought_pb2.Thought,
    dst: thought_pb2.Thought,
    edge_type: int,
    *,
    model_confidence: float = 1.0,
) -> float:
    """`entity_overlap × edge_type_prior`, per claude_context.md §6, with the
    overlap term floored and LLM edges additionally scaled by the model's
    stated confidence.
    """
    overlap = max(entity_overlap(src, dst), _OVERLAP_FLOOR)
    prior = EDGE_TYPE_PRIOR.get(edge_type, 0.5)
    return round(overlap * prior * model_confidence, 6)


def temporal_edges(thoughts: list[thought_pb2.Thought]) -> list[thought_pb2.ThoughtEdge]:
    """The rule layer: one edge from each thought to the next thought in the
    conversation, in turn order. Zero tokens, zero model calls.

    Consecutive *thoughts*, not consecutive *turns*: several thoughts from
    one turn are chained in emission order, so the backbone stays a single
    path through the whole consultation and every thought is reachable from
    every other by temporal edges alone. That connectedness is what stops
    graph retrieval from returning an empty neighbourhood when the LLM layer
    predicts nothing for a sparsely-related thought.

    Direction is always earlier → later. A dialogue has an arrow of time and
    the graph should carry it: "the pain started, then I took ibuprofen" is
    not the same clinical claim as its reverse.
    """
    ordered = sorted(thoughts, key=lambda t: (t.turn_index, t.char_start, t.id))
    edges = []
    for src, dst in zip(ordered, ordered[1:], strict=False):
        edges.append(
            thought_pb2.ThoughtEdge(
                consultation_id=src.consultation_id,
                run_config_id=src.run_config_id,
                src_thought_id=src.id,
                dst_thought_id=dst.id,
                edge_type=EdgeT.EDGE_TYPE_TEMPORAL,
                weight=edge_weight(src, dst, EdgeT.EDGE_TYPE_TEMPORAL),
                predicted_by=thought_pb2.PredictedBy.PREDICTED_BY_RULE,
                rationale="consecutive in turn order",
            )
        )
    return edges


def render_thoughts_for_edges(thoughts: list[thought_pb2.Thought]) -> str:
    """The thought list the edge prompt sees. Deliberately compact: this
    block is sent once per consultation and the whole point of predicting
    edges over thoughts rather than over raw turns is that thoughts are
    already the distilled form. Includes polarity because a negation edge is
    unpredictable without it, and speaker because who denied what is the
    difference between a patient changing their story and a doctor
    correcting the record.
    """
    lines = []
    for t in thoughts:
        entities = ", ".join(e.text for e in t.entities)
        anchor = f", {t.temporal_anchor}" if t.temporal_anchor else ""
        lines.append(
            f"{t.id}  [turn {t.turn_index}, {SPEAKER_NAME.get(t.speaker, 'unknown')}, "
            f"{CATEGORY_NAME.get(t.category, 'other')}, "
            f"{POLARITY_NAME.get(t.polarity, 'asserted')}{anchor}]  "
            f"{t.text}" + (f"  (entities: {entities})" if entities else "")
        )
    return "\n".join(lines)


async def predict_llm_edges(
    *,
    conn: psycopg.AsyncConnection,
    llm_client: LLMClient,
    model: str,
    thoughts: list[thought_pb2.Thought],
    consultation_id: str,
    run_config_id: str,
    repair_max_attempts: int,
    timeout_s: float,
    language: str = prompts.DEFAULT_LANGUAGE,
) -> tuple[list[thought_pb2.ThoughtEdge], int, int, int, int, int]:
    """Returns (edges, tokens_in, tokens_out, llm_calls, cache_hits,
    repair_attempts)."""
    if len(thoughts) < 2:
        # One thought cannot have a relation to anything. Skipping the call
        # is not an optimization detail: a model asked to relate a single
        # item to itself will invent a self-edge to fill the schema.
        return [], 0, 0, 0, 0, 0

    pr = prompts.load_edge_prediction_prompts(language=language)
    by_id = {t.id: t for t in thoughts}
    base_user = pr.user_template.format(thought_list=render_thoughts_for_edges(thoughts))
    user_prompt = base_user
    tokens_in = tokens_out = cache_hits = repair_attempts = 0
    last_error = ""
    last_content = ""
    max_tokens = OTPM_SAFE_MAX_TOKENS

    for attempt in range(repair_max_attempts + 1):
        completion, cache_hit = await complete_cached(
            conn,
            llm_client,
            model=model,
            system_prompt=pr.system,
            user_prompt=user_prompt,
            temperature=0.0,
            timeout_s=timeout_s,
            json_mode=True,
            max_tokens=max_tokens,
        )
        tokens_in += completion.tokens_in
        tokens_out += completion.tokens_out
        if cache_hit:
            cache_hits += 1

        result = schema.validate_edge_prediction(completion.content, known_ids=set(by_id))
        if result.valid:
            edges = []
            for item in result.items:
                src = by_id[str(item["src_thought_id"])]
                dst = by_id[str(item["dst_thought_id"])]
                edge_type = _EDGE_TYPE_BY_NAME[str(item["edge_type"])]
                confidence = float(item["confidence"])  # type: ignore[arg-type]
                weight = edge_weight(src, dst, edge_type, model_confidence=confidence)
                if weight < MIN_EDGE_WEIGHT:
                    continue
                edges.append(
                    thought_pb2.ThoughtEdge(
                        consultation_id=consultation_id,
                        run_config_id=run_config_id,
                        src_thought_id=src.id,
                        dst_thought_id=dst.id,
                        edge_type=edge_type,
                        weight=weight,
                        predicted_by=thought_pb2.PredictedBy.PREDICTED_BY_LLM,
                        rationale=str(item.get("rationale", "")),
                    )
                )
            return edges, tokens_in, tokens_out, attempt + 1, cache_hits, repair_attempts

        last_error = result.error
        last_content = completion.content
        logger.warning(
            "edge prediction validation failed",
            extra={
                "extra_fields": {
                    "consultation_id": consultation_id,
                    "attempt": attempt,
                    "error": last_error,
                }
            },
        )
        if attempt >= repair_max_attempts:
            break
        repair_attempts += 1
        user_prompt = base_user + pr.repair_addendum_template.format(
            validation_error=last_error, previous_response=last_content
        )

    raise FatalError(
        f"edge prediction did not produce valid output after {repair_attempts} repair "
        f"attempt(s); last error: {last_error}",
        code="EDGE_PREDICTION_INVALID",
    )


async def assemble_edges(
    *,
    conn: psycopg.AsyncConnection,
    llm_client: LLMClient,
    model: str,
    thoughts: list[thought_pb2.Thought],
    consultation_id: str,
    run_config_id: str,
    repair_max_attempts: int,
    timeout_s: float,
    language: str = prompts.DEFAULT_LANGUAGE,
) -> EdgeAssemblyResult:
    """Rule layer plus LLM layer, deduplicated.

    On a conflict for the same ordered pair, the LLM edge wins over the
    temporal one: a pair that is both adjacent in the conversation and
    causally related is more usefully described as causal, and keeping both
    would double-count that pair's weight during retrieval. The temporal
    backbone is only there to guarantee connectedness, so losing one of its
    links to a stronger, more specific relation costs nothing.
    """
    rule = temporal_edges(thoughts)
    llm_edges, t_in, t_out, calls, hits, repairs = await predict_llm_edges(
        conn=conn,
        llm_client=llm_client,
        model=model,
        thoughts=thoughts,
        consultation_id=consultation_id,
        run_config_id=run_config_id,
        repair_max_attempts=repair_max_attempts,
        timeout_s=timeout_s,
        language=language,
    )

    by_pair: dict[tuple[str, str], thought_pb2.ThoughtEdge] = {}
    for edge in rule:
        by_pair[(edge.src_thought_id, edge.dst_thought_id)] = edge
    superseded = 0
    for edge in llm_edges:
        key = (edge.src_thought_id, edge.dst_thought_id)
        if key in by_pair:
            superseded += 1
        by_pair[key] = edge

    edges = list(by_pair.values())
    rule_count = sum(
        1 for e in edges if e.predicted_by == thought_pb2.PredictedBy.PREDICTED_BY_RULE
    )
    logger.info(
        "thought graph assembled",
        extra={
            "extra_fields": {
                "consultation_id": consultation_id,
                "run_config_id": run_config_id,
                "thought_count": len(thoughts),
                "edge_count": len(edges),
                "rule_edges": rule_count,
                "llm_edges": len(edges) - rule_count,
                "temporal_edges_superseded_by_llm": superseded,
            }
        },
    )
    return EdgeAssemblyResult(
        edges=edges,
        tokens_in=t_in,
        tokens_out=t_out,
        llm_calls=calls,
        cache_hits=hits,
        repair_attempts=repairs,
        rule_edge_count=rule_count,
        llm_edge_count=len(edges) - rule_count,
    )


__all__ = [
    "EDGE_TYPE_PRIOR",
    "EDGE_TYPE_NAME",
    "MIN_EDGE_WEIGHT",
    "EdgeAssemblyResult",
    "entity_overlap",
    "edge_weight",
    "temporal_edges",
    "predict_llm_edges",
    "assemble_edges",
    "render_thoughts_for_edges",
]
