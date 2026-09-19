"""Module 5's scorer — GoT-HCS Eq. 19-22's intent without trained heads
(claude_context.md §6, "Multi-stage reasoning" row).

The paper learns three scorer heads. We have no labeled downstream signal to
train them on, so each is replaced by a computable equivalent that measures
the same thing:

| Paper head  | Ours                                                          |
|-------------|---------------------------------------------------------------|
| Relevance   | Entity overlap between the candidate and its supporting subgraph |
| Consistency | Embedding similarity to the source thoughts, minus a contradiction penalty |
| Redundancy  | n-gram overlap against content already selected for other fields |

Two of the three cost **zero API calls** (§8's mitigation #2), which is what
makes a 4-arm ablation affordable at all. An `llm_judge` backend with a
written rubric is available as an alternative or an addition, selected by
`RunConfig.scorer_backend` — when set to `both`, each candidate carries two
`ScoreBreakdown`s and the report can ask whether the free scorer and the paid
one agree, which is itself a result.

## Sign convention, stated once

`relevance` and `consistency` are higher-is-better in [0, 1]. **`redundancy`
is raw measured overlap and is higher-is-WORSE.** The aggregate is therefore

    aggregate = w_rel*relevance + w_con*consistency - w_red*redundancy

Storing raw overlap rather than `1 - overlap` is deliberate (decision #95):
the number goes into `extractions.score_breakdown` and will be read years
later when the report is written, and a persisted `redundancy: 0.9` must mean
"this repeated almost everything", not its opposite. The sign lives here, in
one documented place, instead of in every reader's head.

## The contradiction penalty

Consistency is not pure similarity. A candidate can be lexically close to its
supporting thoughts while asserting the opposite of what they say — which is
precisely the failure the whole polarity design (decision #89) exists to
catch. A candidate that states content its supporting thoughts *negate*, with
no hedging, is penalized directly rather than merely scoring a little lower.
This is the one clinically-loaded term in the scorer, so it is reported
separately on the breakdown (`contradiction_penalty`) instead of being
folded silently into `consistency`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import psycopg

from coda.v1 import got_pb2, runconfig_pb2, thought_pb2
from nlp_service import prompts
from nlp_service.graph.thoughts import POLARITY_NAME
from nlp_service.llm.client import LLMClient
from nlp_service.llm_cache import complete_cached
from nlp_service.reasoning import schema as rschema
from nlp_service.reasoning.embeddings import EmbeddingBackend, resolve_backend, tokenize

logger = logging.getLogger(__name__)

SCORER_HEURISTIC = "heuristic"
SCORER_LLM_JUDGE = "llm_judge"
SCORER_BOTH = "both"
VALID_SCORER_BACKENDS = (SCORER_HEURISTIC, SCORER_LLM_JUDGE, SCORER_BOTH)

DEFAULT_WEIGHTS = runconfig_pb2.ScorerWeights(relevance=0.5, consistency=0.3, redundancy=0.2)
"""Recorded 2026-09-04 (decision #95) — no defaults existed anywhere before.

Relevance dominates because it is the only criterion measuring whether the
candidate actually answers the field from its own evidence, which is what H1
is about. Redundancy is smallest because it is housekeeping, not quality: a
heavy redundancy weight rewards terse, information-poor candidates that score
well by saying little, which is the opposite of what a clinical note needs.
"""

NGRAM_N = 3
"""Trigrams for the redundancy overlap. Unigrams flag any shared clinical
vocabulary as repetition — every field of one consultation talks about the
same patient — while 4-grams+ miss real restatement that has been lightly
reworded between fields."""

CONTRADICTION_PENALTY = 0.5
"""Subtracted from consistency when a candidate asserts what its supporting
thoughts negate. Deliberately large: halving a candidate's consistency is
meant to lose it the selection, because writing a ruled-out symptom into a
clinical note is a worse error than omitting a real one."""


@dataclass(frozen=True, slots=True)
class ScoringContext:
    """Everything the scorers need about one field's evidence."""

    field_key: str
    thoughts: list[thought_pb2.Thought]
    already_selected: list[str] = field(default_factory=list)
    """Text already selected for OTHER fields this consultation, which is
    what redundancy is measured against."""


def _entities(thoughts: list[thought_pb2.Thought]) -> set[str]:
    return {e.text.strip().lower() for t in thoughts for e in t.entities if e.text.strip()}


def relevance_score(candidate_text: str, ctx: ScoringContext) -> float:
    """Fraction of the supporting subgraph's entities the candidate mentions.

    Recall-oriented rather than Jaccard on purpose: a candidate covering more
    of the field's evidence should score higher, and penalizing it for
    additional words (which Jaccard's denominator would) would push toward
    sparse answers. A field whose subgraph names no entities at all scores
    0.0 — there is nothing to be relevant *to*, and the candidate should not
    be rewarded for that.
    """
    subgraph_entities = _entities(ctx.thoughts)
    if not subgraph_entities:
        return 0.0
    text = candidate_text.lower()
    hit = sum(1 for e in subgraph_entities if e in text)
    return hit / len(subgraph_entities)


def contradiction_penalty(candidate_text: str, ctx: ScoringContext) -> float:
    """Penalize a candidate that asserts content its supporting thoughts
    negate.

    The check is deliberately narrow. It fires only when a NEGATED thought's
    entities appear in the candidate and the candidate carries no negation
    cue of its own — i.e. the candidate mentions the ruled-out thing as
    though it were true. A candidate that correctly writes "no penicillin
    allergy" mentions the same entities but carries a cue, and is not
    penalized. Being narrow matters more than being thorough here: a false
    positive would suppress a *correct* candidate that properly records a
    pertinent negative, which is exactly the content clinicians most want
    preserved.
    """
    negated = [t for t in ctx.thoughts if t.polarity == thought_pb2.Polarity.POLARITY_NEGATED]
    if not negated:
        return 0.0
    text = candidate_text.lower()
    tokens = set(tokenize(candidate_text))
    has_negation_cue = bool(tokens & _NEGATION_CUES)
    if has_negation_cue:
        return 0.0
    for t in negated:
        for e in t.entities:
            term = e.text.strip().lower()
            if term and term in text:
                return CONTRADICTION_PENALTY
    return 0.0


_NEGATION_CUES = frozenset(
    {
        "no", "not", "none", "never", "without", "denies", "denied", "negative",
        "absent", "ruled", "excluded", "nil", "n0ne", "unremarkable", "free",
    }
)
"""Surface cues that a candidate is itself expressing a negation. English
only for v1 — and this is a real v2 extension point: a Kannada-English
transcript will express negation with cues absent from this set, so the set
must become language-keyed alongside the prompt directories
(claude_context.md §2.1)."""


def consistency_score(
    candidate_text: str, ctx: ScoringContext, *, backend: EmbeddingBackend
) -> tuple[float, float]:
    """Returns `(consistency, contradiction_penalty)`.

    Similarity is the MAX over supporting thoughts, not the mean. A field's
    subgraph legitimately contains thoughts about different aspects (a
    symptom, its duration, its trigger); a good candidate is strongly
    grounded in *some* of them, and averaging would punish it for the ones it
    does not restate.
    """
    if not ctx.thoughts:
        return 0.0, 0.0
    sims = backend.similarity(candidate_text, [t.text for t in ctx.thoughts])
    base = max(sims) if sims else 0.0
    penalty = contradiction_penalty(candidate_text, ctx)
    return max(0.0, base - penalty), penalty


def _ngrams(text: str, n: int = NGRAM_N) -> set[tuple[str, ...]]:
    toks = tokenize(text)
    if len(toks) < n:
        return {tuple(toks)} if toks else set()
    return {tuple(toks[i : i + n]) for i in range(len(toks) - n + 1)}


def redundancy_score(candidate_text: str, ctx: ScoringContext) -> float:
    """RAW trigram overlap against already-selected content. Higher is worse.

    Measured as the fraction of the *candidate's* n-grams that already appear
    elsewhere, so a long candidate is not penalized simply for being long —
    only for the proportion of itself that is restatement.
    """
    if not ctx.already_selected:
        return 0.0
    cand = _ngrams(candidate_text)
    if not cand:
        return 0.0
    prior: set[tuple[str, ...]] = set()
    for text in ctx.already_selected:
        prior |= _ngrams(text)
    if not prior:
        return 0.0
    return len(cand & prior) / len(cand)


def aggregate(
    *, relevance: float, consistency: float, redundancy: float, weights: runconfig_pb2.ScorerWeights
) -> float:
    """`w_rel*rel + w_con*con - w_red*red` — see the module docstring's sign
    convention. Redundancy is SUBTRACTED because it is stored raw."""
    return (
        weights.relevance * relevance
        + weights.consistency * consistency
        - weights.redundancy * redundancy
    )


def resolve_weights(rc: runconfig_pb2.RunConfig) -> runconfig_pb2.ScorerWeights:
    """A run config with no scorer weights set gets the recorded defaults.

    Proto3 scalars default to 0.0 with no way to distinguish "unset" from
    "deliberately zero", so an all-zero triple is treated as unset. A config
    genuinely wanting every weight at zero has no meaningful selection
    criterion anyway.
    """
    w = rc.scorer_weights
    if w.relevance == 0.0 and w.consistency == 0.0 and w.redundancy == 0.0:
        return DEFAULT_WEIGHTS
    return w


def score_heuristic(
    candidate: got_pb2.Candidate,
    ctx: ScoringContext,
    *,
    weights: runconfig_pb2.ScorerWeights,
    backend: EmbeddingBackend | None = None,
) -> got_pb2.ScoreBreakdown:
    """The zero-API scorer. No connection, no client, no await — which is the
    strongest available statement that it costs nothing."""
    emb = backend or resolve_backend()
    rel = relevance_score(candidate.text, ctx)
    con, penalty = consistency_score(candidate.text, ctx, backend=emb)
    red = redundancy_score(candidate.text, ctx)
    return got_pb2.ScoreBreakdown(
        relevance=rel,
        consistency=con,
        redundancy=red,
        aggregate=aggregate(
            relevance=rel, consistency=con, redundancy=red, weights=weights
        ),
        scorer_backend=SCORER_HEURISTIC,
        consistency_backend=emb.name,
        contradiction_penalty=penalty,
    )


async def score_llm_judge(
    candidates: list[got_pb2.Candidate],
    ctx: ScoringContext,
    *,
    conn: psycopg.AsyncConnection,
    llm_client: LLMClient,
    judge_model: str,
    weights: runconfig_pb2.ScorerWeights,
    timeout_s: float,
    language: str = prompts.DEFAULT_LANGUAGE,
) -> tuple[list[got_pb2.ScoreBreakdown], int, int, int, int]:
    """Rubric-based scoring on `judge_model`. Returns
    `(breakdowns, tokens_in, tokens_out, llm_calls, cache_hits)`.

    One call scores ALL candidates for a field, not one call each: the judge
    has to compare them anyway, and N separate calls would both cost N times
    as much and produce scores calibrated independently — which is worse for
    a task whose only real question is "which of these is best".

    On any failure the judge returns zeroed breakdowns rather than raising.
    Scoring is an ordering mechanism; if the judge is unavailable, selection
    should fall back to the free scorer, not fail the consultation.
    """
    if not candidates:
        return [], 0, 0, 0, 0

    pr = prompts.load_judge_prompts(language=language)
    subgraph = "\n".join(
        f"- [{POLARITY_NAME.get(t.polarity, 'asserted')}, turn {t.turn_index}] {t.text}"
        for t in ctx.thoughts
    ) or "(no supporting thoughts were retrieved for this field)"
    rendered = "\n".join(
        f"{i}. {c.text}" for i, c in enumerate(candidates)
    )
    user_prompt = pr.user_template.format(
        field_key=ctx.field_key, subgraph=subgraph, candidates=rendered
    )

    completion, cache_hit = await complete_cached(
        conn,
        llm_client,
        model=judge_model,
        system_prompt=pr.system,
        user_prompt=user_prompt,
        temperature=0.0,
        timeout_s=timeout_s,
        json_mode=True,
        # A reasoning judge can spend its whole budget on hidden reasoning
        # before emitting content — decision #75's failure, reproduced here.
        max_tokens=max(1500, 400 * len(candidates)),
    )
    result = rschema.validate_judge_scores(completion.content, n_candidates=len(candidates))
    if not result.valid:
        logger.warning(
            "llm judge returned unusable scores; falling back to zeroed breakdowns",
            extra={
                "extra_fields": {
                    "field_key": ctx.field_key,
                    "judge_model": judge_model,
                    "error": result.error,
                }
            },
        )
        return (
            [
                got_pb2.ScoreBreakdown(
                    scorer_backend=SCORER_LLM_JUDGE,
                    judge_model=judge_model,
                    judge_rationale=f"judge output unusable: {result.error}",
                )
                for _ in candidates
            ],
            completion.tokens_in,
            completion.tokens_out,
            1,
            1 if cache_hit else 0,
        )

    breakdowns = []
    for item in result.items:
        rel = float(item["relevance"])  # type: ignore[arg-type]
        con = float(item["consistency"])  # type: ignore[arg-type]
        red = float(item["redundancy"])  # type: ignore[arg-type]
        breakdowns.append(
            got_pb2.ScoreBreakdown(
                relevance=rel,
                consistency=con,
                redundancy=red,
                aggregate=aggregate(
                    relevance=rel, consistency=con, redundancy=red, weights=weights
                ),
                scorer_backend=SCORER_LLM_JUDGE,
                judge_model=judge_model,
                judge_rationale=str(item.get("rationale", "")),
            )
        )
    return (
        breakdowns,
        completion.tokens_in,
        completion.tokens_out,
        1,
        1 if cache_hit else 0,
    )


__all__ = [
    "SCORER_HEURISTIC",
    "SCORER_LLM_JUDGE",
    "SCORER_BOTH",
    "VALID_SCORER_BACKENDS",
    "DEFAULT_WEIGHTS",
    "CONTRADICTION_PENALTY",
    "ScoringContext",
    "relevance_score",
    "consistency_score",
    "redundancy_score",
    "contradiction_penalty",
    "aggregate",
    "resolve_weights",
    "score_heuristic",
    "score_llm_judge",
]
