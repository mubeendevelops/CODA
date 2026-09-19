"""Graph-structured context retrieval — the substitute for GoT-HCS's trained
graph attention network (claude_context.md §6, "GAT (attention propagation)"
row; ADR-0011).

The paper propagates information with an 8-head trained GAT, learning which
neighbours matter for a downstream objective. Training that needs many graphs
with labeled downstream signal, which this project does not have. What a GAT
ultimately decides is *which thoughts influence the answer, and how strongly*,
so we decide the same thing structurally instead of learnedly: for a target
clinical field, walk the thought graph outward from the thoughts whose
category maps to that field, and let the resulting neighbourhood — ordered by
accumulated edge weight — be the context the generation prompt receives.

This is stated in the report as an explicit substitution, not as an
equivalent. What is lost is the learned part: the weights are priors
(edges.py's `EDGE_TYPE_PRIOR`) rather than fitted parameters. What is kept is
the mechanism the priors feed: graph structure decides both membership and
aggregation strength, so a thought two weakly-linked hops away contributes
less than a directly, strongly-linked one, exactly as attention would.

It is also, per claude_context.md §8's mitigation #3, the pipeline's largest
single token saving: sending a field's neighbourhood instead of the whole
transcript is what keeps a 4-arm ablation inside the free-tier budget. That
is why the budget is a first-class parameter here and not an afterthought.

## The traversal and budget policy, in full

1. **Seed.** Select thoughts whose `clinical_category` is in
   `FIELD_SEED_CATEGORIES[field]` — a fixed rule mapping, the same one
   hierarchical distillation uses (claude_context.md §6's "Hierarchical
   distillation" row: the 8-field taxonomy is fixed, so learned clustering
   would solve a problem we do not have). Seeds enter at depth 0 with
   priority 1.0.

2. **Expand.** Breadth-first over both edge directions, to `max_depth`
   (default 2). A neighbour's priority is `parent_priority × edge.weight ×
   DIRECTION_DECAY`, where traversing an edge backwards costs an extra
   factor — a thought that elaborates a seed is more relevant to that seed
   than an arbitrary thought the seed elaborates. Depth 2 is the default
   because depth 1 misses the standard dialogue pattern where a symptom's
   detail arrives via an intermediate clarifying exchange, while depth 3 on
   a temporally-chained graph reaches most of the consultation and gives
   back the token saving the mechanism exists to produce.

3. **Guaranteed edges.** `NEGATION` and `COREFERENCE` edges incident to any
   already-selected thought are always followed, regardless of depth or
   remaining budget, and their targets are admitted ahead of everything else.
   Without this rule a negation could be truncated away from the thought it
   negates by a budget cut, and the generation prompt would then contain an
   asserted symptom with no trace of its retraction — which is not a smaller
   context, it is a wrong one. This is the single most important line in the
   policy and it is why the budget is enforced *after* the guaranteed set,
   never over it.

4. **Order and truncate.** Sort the selected thoughts by priority descending,
   then by `(turn_index, char_start)` ascending to break ties, and admit in
   that order until `token_budget` is reached, estimated by
   `estimate_tokens`. Guaranteed thoughts and seeds are admitted first and
   are never truncated; if seeds alone exceed the budget, the budget is
   exceeded and the overflow is reported rather than the seeds being
   dropped — silently discarding the field's own evidence to hit a token
   number would be a worse failure than a larger prompt.

5. **Emit in reading order.** The returned context is re-sorted into
   `(turn_index, char_start)` order regardless of priority, because a
   generation model reads a consultation better chronologically than by
   relevance rank, and the chronology is free information we already have.

Every knob is a plain number on `RetrievalPolicy`, so a run config can record
what was used and an ablation can vary it.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field

from coda.v1 import thought_pb2
from nlp_service.graph.thoughts import CATEGORY_NAME, POLARITY_NAME, SPEAKER_NAME

logger = logging.getLogger(__name__)

Cat = thought_pb2.ThoughtCategory
EdgeT = thought_pb2.EdgeType

FIELD_SEED_CATEGORIES: dict[str, tuple[int, ...]] = {
    "chief_complaint": (Cat.THOUGHT_CATEGORY_SYMPTOM,),
    "hopi": (Cat.THOUGHT_CATEGORY_SYMPTOM,),
    "past_medical_history": (Cat.THOUGHT_CATEGORY_HISTORY,),
    "medications": (Cat.THOUGHT_CATEGORY_MEDICATION,),
    "allergies": (Cat.THOUGHT_CATEGORY_ALLERGY,),
    "examination_findings": (Cat.THOUGHT_CATEGORY_EXAMINATION,),
    "provisional_diagnosis": (
        Cat.THOUGHT_CATEGORY_DIAGNOSIS,
        Cat.THOUGHT_CATEGORY_SYMPTOM,
    ),
    "investigations_advised": (Cat.THOUGHT_CATEGORY_INVESTIGATION,),
    "treatment_plan": (
        Cat.THOUGHT_CATEGORY_PLAN,
        Cat.THOUGHT_CATEGORY_MEDICATION,
    ),
}
"""Field key to seed categories. Keys match `nlp_service.db.FIELD_KEYS` (the
8 fields with medications/allergies split, mirroring migration 000019's
CHECK).

Two fields take a second category deliberately: a provisional diagnosis is
argued from symptoms, and a treatment plan is largely prescriptions. Seeding
those from one category each would leave the traversal to recover the
connection by luck of edge prediction, when the taxonomy already states it.
"""

GUARANTEED_EDGE_TYPES = frozenset({EdgeT.EDGE_TYPE_NEGATION, EdgeT.EDGE_TYPE_COREFERENCE})
"""Followed unconditionally — see policy step 3. Dropping one of these does
not shrink the context, it corrupts it."""


@dataclass(frozen=True, slots=True)
class RetrievalPolicy:
    max_depth: int = 2
    token_budget: int = 900
    """Per field. Eight fields at this budget is roughly 7.2K of context per
    candidate-generation pass, against claude_context.md §8's ~7.5K estimate
    for that stage."""
    direction_decay: float = 0.7
    """Multiplier applied when traversing an edge against its direction."""
    min_priority: float = 0.02
    """Prune below this. Stops the temporal backbone (prior 0.3, so ~0.075
    per hop at the overlap floor) from crawling the entire consultation at
    depth 2 with negligible priority."""
    max_thoughts: int = 40
    """Hard ceiling independent of the token budget, so a pathological graph
    cannot produce an unbounded prompt."""


@dataclass(frozen=True, slots=True)
class RetrievedContext:
    field_key: str
    thoughts: list[thought_pb2.Thought]
    """In reading order (turn_index, char_start) — see policy step 5."""
    priority: dict[str, float] = field(default_factory=dict)
    seed_ids: tuple[str, ...] = ()
    guaranteed_ids: tuple[str, ...] = ()
    estimated_tokens: int = 0
    truncated: int = 0
    """How many reachable thoughts were dropped by budget or ceiling. Non-zero
    is normal; it is reported so the eval can see whether a field's answer was
    generated from a truncated view."""
    budget_exceeded: bool = False
    """True when the guaranteed/seed set alone exceeded `token_budget`. The
    context is correct and over budget, rather than on budget and wrong."""


def estimate_tokens(text: str) -> int:
    """~4 characters per token, the standard rough English ratio.

    Deliberately not a real tokenizer: the budget is a coarse guard against
    unbounded prompts, and pulling in a model-specific tokenizer would add a
    dependency, tie the budget to one provider's vocabulary, and make the
    number drift the moment the model changes (which it already has once —
    decision #71). An estimate that is stable across models is worth more
    here than one that is exact for one of them.
    """
    return max(1, (len(text) + 3) // 4)


def render_thought_line(t: thought_pb2.Thought) -> str:
    """One thought as it appears in a generation prompt. Carries polarity
    explicitly and in words: a downstream model shown "no chest pain" as bare
    text may still write "chest pain" into the note, and the whole reason
    polarity is modelled is to stop that.
    """
    anchor = f", {t.temporal_anchor}" if t.temporal_anchor else ""
    entities = ", ".join(e.text for e in t.entities)
    line = (
        f"[turn {t.turn_index}, {SPEAKER_NAME.get(t.speaker, 'unknown')}, "
        f"{CATEGORY_NAME.get(t.category, 'other')}, "
        f"{POLARITY_NAME.get(t.polarity, 'asserted')}{anchor}] {t.text}"
    )
    if entities:
        line += f" (entities: {entities})"
    return line


def render_context(ctx: RetrievedContext) -> str:
    return "\n".join(render_thought_line(t) for t in ctx.thoughts)


def _adjacency(
    edges: list[thought_pb2.ThoughtEdge],
) -> dict[str, list[tuple[str, thought_pb2.ThoughtEdge, bool]]]:
    """thought_id -> [(neighbour_id, edge, forward)]. Both directions, with
    `forward` recording which way the edge actually points so the traversal
    can charge the backwards decay."""
    adj: dict[str, list[tuple[str, thought_pb2.ThoughtEdge, bool]]] = {}
    for e in edges:
        adj.setdefault(e.src_thought_id, []).append((e.dst_thought_id, e, True))
        adj.setdefault(e.dst_thought_id, []).append((e.src_thought_id, e, False))
    return adj


def retrieve_field_context(
    *,
    field_key: str,
    thoughts: list[thought_pb2.Thought],
    edges: list[thought_pb2.ThoughtEdge],
    policy: RetrievalPolicy | None = None,
) -> RetrievedContext:
    """Select the subgraph neighbourhood that becomes generation context for
    one clinical field. See this module's docstring for the full policy.
    """
    policy = policy or RetrievalPolicy()
    if field_key not in FIELD_SEED_CATEGORIES:
        raise KeyError(
            f"graph.retrieval: unknown field_key {field_key!r}; "
            f"expected one of {sorted(FIELD_SEED_CATEGORIES)}"
        )

    by_id = {t.id: t for t in thoughts}
    adj = _adjacency(edges)
    seed_categories = FIELD_SEED_CATEGORIES[field_key]
    seed_ids = [t.id for t in thoughts if t.category in seed_categories]

    priority: dict[str, float] = {tid: 1.0 for tid in seed_ids}
    guaranteed: set[str] = set()

    # --- Steps 2 and 3: weighted BFS, with guaranteed edges followed
    # unconditionally. Guaranteed neighbours inherit their parent's priority
    # undiminished and do not consume depth, so a negation five hops from a
    # seed is still reached the moment its target is selected.
    queue: deque[tuple[str, int, float]] = deque((tid, 0, 1.0) for tid in seed_ids)
    while queue:
        tid, depth, prio = queue.popleft()
        for neighbour_id, edge, forward in adj.get(tid, ()):
            if neighbour_id not in by_id:
                continue
            is_guaranteed = edge.edge_type in GUARANTEED_EDGE_TYPES
            if is_guaranteed:
                next_prio = prio
                next_depth = depth
            else:
                if depth >= policy.max_depth:
                    continue
                decay = 1.0 if forward else policy.direction_decay
                next_prio = prio * edge.weight * decay
                next_depth = depth + 1
                if next_prio < policy.min_priority:
                    continue
            if next_prio <= priority.get(neighbour_id, 0.0):
                # Already reached at least this strongly; re-expanding would
                # only ever lower its priority.
                continue
            priority[neighbour_id] = next_prio
            if is_guaranteed:
                guaranteed.add(neighbour_id)
            queue.append((neighbour_id, next_depth, next_prio))

    # --- Step 4: order, then admit under budget.
    protected = set(seed_ids) | guaranteed

    def rank(tid: str) -> tuple[int, float, int, int]:
        t = by_id[tid]
        return (0 if tid in protected else 1, -priority[tid], t.turn_index, t.char_start)

    ordered = sorted(priority, key=rank)

    selected: list[str] = []
    used = 0
    truncated = 0
    budget_exceeded = False
    for tid in ordered:
        cost = estimate_tokens(render_thought_line(by_id[tid]))
        is_protected = tid in protected
        over_budget = used + cost > policy.token_budget
        over_ceiling = len(selected) >= policy.max_thoughts
        if is_protected:
            # Never truncated. A field's own seed evidence and any negation
            # or coreference reaching it are admitted even past budget, and
            # the overflow is reported instead of hidden.
            if over_budget or over_ceiling:
                budget_exceeded = True
        elif over_budget or over_ceiling:
            truncated += 1
            continue
        selected.append(tid)
        used += cost

    if budget_exceeded:
        logger.warning(
            "graph retrieval exceeded token budget on protected thoughts",
            extra={
                "extra_fields": {
                    "field_key": field_key,
                    "token_budget": policy.token_budget,
                    "estimated_tokens": used,
                    "protected_count": len(protected),
                }
            },
        )

    # --- Step 5: emit chronologically.
    selected.sort(key=lambda tid: (by_id[tid].turn_index, by_id[tid].char_start, tid))
    return RetrievedContext(
        field_key=field_key,
        thoughts=[by_id[tid] for tid in selected],
        priority={tid: priority[tid] for tid in selected},
        seed_ids=tuple(seed_ids),
        guaranteed_ids=tuple(sorted(guaranteed)),
        estimated_tokens=used,
        truncated=truncated,
        budget_exceeded=budget_exceeded,
    )


def retrieve_all_fields(
    *,
    thoughts: list[thought_pb2.Thought],
    edges: list[thought_pb2.ThoughtEdge],
    policy: RetrievalPolicy | None = None,
) -> dict[str, RetrievedContext]:
    """One retrieval per clinical field, which is what a candidate-generation
    pass batched across the 8 fields needs (claude_context.md §8 mitigation
    #4).
    """
    return {
        field_key: retrieve_field_context(
            field_key=field_key, thoughts=thoughts, edges=edges, policy=policy
        )
        for field_key in FIELD_SEED_CATEGORIES
    }


__all__ = [
    "FIELD_SEED_CATEGORIES",
    "GUARANTEED_EDGE_TYPES",
    "RetrievalPolicy",
    "RetrievedContext",
    "estimate_tokens",
    "render_thought_line",
    "render_context",
    "retrieve_field_context",
    "retrieve_all_fields",
]
