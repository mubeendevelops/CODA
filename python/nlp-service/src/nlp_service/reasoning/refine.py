"""Module 5's select-and-refine loop: pick the best candidate, then critique
and regenerate it K times against its own subgraph.

Every iteration is persisted, including the ones that lose. `RefinementTrace`
carries the full `CandidateSet` per iteration plus an explicit
`score_trajectory`, because the honest question about this whole mechanism is
whether refinement actually improves anything — and a flat or falling
trajectory is a real, reportable, negative result (plan.md Phase 6 AC7). A
design that only kept the final answer could not distinguish "refinement
helped" from "refinement ran".

## The regression guard

A refinement pass can make a field worse: the critique prompt invites the
model to find fault, and a model asked to find fault will sometimes invent
one. Each iteration's revision is therefore re-scored and **only adopted if
it scores at least as well as what it replaces**. A revision that scores
lower is recorded in the trace — so the trajectory shows the attempt — and
discarded from selection.

This is not a way of hiding bad refinements. It is the difference between
"K=2 sometimes hurts" (a property of the method, which the trace still shows)
and "K=2 sometimes hurts the delivered note" (a bug we would be shipping).
The ablation still measures the former, since `got_k1` vs `got_k2` compares
final notes produced under the same guard.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import psycopg

from coda.v1 import got_pb2, runconfig_pb2, thought_pb2
from coda_worker_sdk.errors import FatalError
from nlp_service import prompts
from nlp_service.graph.thoughts import POLARITY_NAME
from nlp_service.llm.client import LLMClient
from nlp_service.llm.limits import SINGLE_FIELD_MAX_TOKENS
from nlp_service.llm_cache import complete_cached
from nlp_service.reasoning import schema as rschema
from nlp_service.reasoning import scoring
from nlp_service.reasoning.embeddings import EmbeddingBackend
from nlp_service.reasoning.generation import LIST_FIELDS

logger = logging.getLogger(__name__)

## max_tokens history (2026-09-05): see nlp_service.llm.limits' docstring —
## first scaled up per-thought to stop hidden reasoning from starving the
## answer, then found that Groq's separate OTPM per-minute ceiling rejects a
## large max_tokens outright; `reasoning_effort: "none"` (client-level) fixed
## the original problem. One field's real citations can still run to several
## hundred tokens, so this uses SINGLE_FIELD_MAX_TOKENS (paired with a
## 6-citation cap in the prompt and CANDIDATE_FIELD_SCHEMA) rather than the
## larger OTPM_SAFE_MAX_TOKENS a whole-graph call needs — see point 3 of
## that docstring.


@dataclass(slots=True)
class FieldOutcome:
    """One field's complete generate/score/refine history and final answer."""

    field_key: str
    trace: got_pb2.RefinementTrace
    selected: got_pb2.Candidate
    score: got_pb2.ScoreBreakdown
    secondary_score: got_pb2.ScoreBreakdown | None = None
    source_thought_ids: list[str] = field(default_factory=list)
    turn_ids: list[int] = field(default_factory=list)
    """Transcript `turn_index` values behind the selected candidate — the
    provenance every field of the final note must carry (claude_context.md
    §3: `{ value, source_turn_ids[], confidence }`)."""
    confidence: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    llm_calls: int = 0
    cache_hits: int = 0


def select_best(
    candidates: list[got_pb2.Candidate], scores: list[got_pb2.ScoreBreakdown]
) -> int:
    """Highest aggregate wins; ties break toward the lower index, which is the
    earlier (more conservative) variant. Deterministic by construction —
    architecture.md §6.3 requires the run be reconstructible, and a tie broken
    arbitrarily would not be.
    """
    if not candidates:
        raise ValueError("select_best called with no candidates")
    best = 0
    for i in range(1, len(scores)):
        if scores[i].aggregate > scores[best].aggregate:
            best = i
    return best


def _payload_to_candidate(
    payload: dict[str, object],
    *,
    field_key: str,
    index: int,
    model: str,
    variant: str,
) -> tuple[got_pb2.Candidate, float]:
    items = rschema.str_list(payload, "items")
    text = ", ".join(items) if field_key in LIST_FIELDS else rschema.scalar_text(payload)
    confidence = rschema.as_float(payload, "confidence")
    return (
        got_pb2.Candidate(
            index=index,
            text=text,
            items=items,
            source_thought_ids=rschema.str_list(payload, "source_thought_ids"),
            generated_by_model=model,
            variant=variant,
            is_refinement=True,
        ),
        confidence,
    )


def render_subgraph(thoughts: list[thought_pb2.Thought]) -> str:
    if not thoughts:
        return "(no supporting thoughts were retrieved for this field)"
    return "\n".join(
        f"- {t.id}  [turn {t.turn_index}, {POLARITY_NAME.get(t.polarity, 'asserted')}] {t.text}"
        for t in thoughts
    )


def _render_candidate(candidate: got_pb2.Candidate, field_key: str) -> str:
    if field_key in LIST_FIELDS:
        if not candidate.items:
            return "(empty list)"
        return "\n".join(f"- {i}" for i in candidate.items)
    return candidate.text or "(empty)"


def _score_detail(score: got_pb2.ScoreBreakdown) -> str:
    parts = [
        f"relevance {score.relevance:.2f}",
        f"consistency {score.consistency:.2f}",
        f"redundancy {score.redundancy:.2f} (higher is worse)",
    ]
    if score.contradiction_penalty:
        parts.append(
            f"contradiction penalty {score.contradiction_penalty:.2f} — the candidate "
            f"may be asserting something its supporting thoughts negate"
        )
    if score.judge_rationale:
        parts.append(f"judge said: {score.judge_rationale}")
    return "Score detail: " + "; ".join(parts) + "."


async def refine_field(
    *,
    conn: psycopg.AsyncConnection,
    llm_client: LLMClient,
    model: str,
    field_key: str,
    thoughts: list[thought_pb2.Thought],
    candidate: got_pb2.Candidate,
    score: got_pb2.ScoreBreakdown,
    ctx: scoring.ScoringContext,
    weights: runconfig_pb2.ScorerWeights,
    embed_backend: EmbeddingBackend,
    k_iterations: int,
    temperature: float,
    repair_max_attempts: int,
    timeout_s: float,
    language: str = prompts.DEFAULT_LANGUAGE,
) -> tuple[
    got_pb2.Candidate, got_pb2.ScoreBreakdown, list[got_pb2.CandidateSet], float, int, int, int, int
]:
    """Runs K critique-and-regenerate passes on one field.

    Returns `(candidate, score, iteration_sets, confidence, tokens_in,
    tokens_out, llm_calls, cache_hits)`. `iteration_sets` covers iterations
    1..K only; iteration 0's set is the caller's generation result.
    """
    pr = prompts.load_refine_prompts(language=language)
    known_ids = {t.id for t in thoughts}
    subgraph = render_subgraph(thoughts)
    shape = "list-valued" if field_key in LIST_FIELDS else "scalar"

    current, current_score = candidate, score
    confidence = 0.0
    sets: list[got_pb2.CandidateSet] = []
    tokens_in = tokens_out = llm_calls = cache_hits = 0

    for iteration in range(1, k_iterations + 1):
        base_user = pr.user_template.format(
            field_key=field_key,
            field_shape=shape,
            subgraph=subgraph,
            iteration=iteration - 1,
            score=f"{current_score.aggregate:.3f}",
            candidate=_render_candidate(current, field_key),
            score_detail=_score_detail(current_score),
        )
        user_prompt = base_user
        revised: got_pb2.Candidate | None = None
        revised_conf = 0.0
        critique = ""
        max_tokens = SINGLE_FIELD_MAX_TOKENS

        for attempt in range(repair_max_attempts + 1):
            # See generation.py's identical wrap for why: Groq can reject the
            # request outright (400, code="GROQ_BAD_REQUEST") rather than
            # return truncated content when the response would have exceeded
            # SINGLE_FIELD_MAX_TOKENS — a "too long" problem the model can fix
            # by being more concise, not a fatal request error, so it is
            # retried via the same repair-addendum mechanism rather than
            # left to propagate and kill the whole refinement pass.
            try:
                completion, cache_hit = await complete_cached(
                    conn,
                    llm_client,
                    model=model,
                    system_prompt=pr.system,
                    user_prompt=user_prompt,
                    temperature=temperature,
                    timeout_s=timeout_s,
                    json_mode=True,
                    max_tokens=max_tokens,
                )
            except FatalError as exc:
                logger.warning(
                    "refinement call rejected outright",
                    extra={
                        "extra_fields": {"field_key": field_key, "iteration": iteration,
                                          "attempt": attempt, "error": str(exc)},
                    },
                )
                if attempt >= repair_max_attempts:
                    break
                user_prompt = base_user + pr.repair_addendum_template.format(
                    validation_error=(
                        "Your previous response was too long and was rejected before any "
                        "content was returned. Be extremely concise: keep the critique to one "
                        "short clause and the revised value as short as possible."
                    ),
                    previous_response="(no response was returned)",
                )
                continue

            tokens_in += completion.tokens_in
            tokens_out += completion.tokens_out
            llm_calls += 1
            if cache_hit:
                cache_hits += 1

            result = rschema.validate_critique(
                completion.content,
                known_thought_ids=known_ids,
                list_field=field_key in LIST_FIELDS,
                field_key=field_key,
            )
            if result.valid:
                assert result.parsed is not None
                critique = str(result.parsed.get("critique", ""))
                payload = result.parsed["revised"]
                assert isinstance(payload, dict)
                revised, revised_conf = _payload_to_candidate(
                    payload,
                    field_key=field_key,
                    index=current.index,
                    model=model,
                    variant=f"{current.variant}+refine{iteration}",
                )
                break
            if attempt >= repair_max_attempts:
                logger.warning(
                    "refinement pass produced no valid revision; keeping the current candidate",
                    extra={
                        "extra_fields": {
                            "field_key": field_key,
                            "iteration": iteration,
                            "error": result.error,
                        }
                    },
                )
                break
            user_prompt = base_user + pr.repair_addendum_template.format(
                validation_error=result.error, previous_response=completion.content
            )

        if revised is None:
            # A failed refinement pass stops the loop for this field rather
            # than retrying blindly at the next K — if the model could not
            # produce a valid revision once, spending another call is
            # unlikely to help and definitely costs quota.
            break

        revised_score = scoring.score_heuristic(
            revised, ctx, weights=weights, backend=embed_backend
        )
        improved = revised_score.aggregate >= current_score.aggregate
        sets.append(
            got_pb2.CandidateSet(
                iteration=iteration,
                candidates=[revised],
                scores=[revised_score],
                selected_index=0 if improved else 0,
            )
        )
        logger.info(
            "refinement iteration complete",
            extra={
                "extra_fields": {
                    "field_key": field_key,
                    "iteration": iteration,
                    "before": round(current_score.aggregate, 4),
                    "after": round(revised_score.aggregate, 4),
                    "adopted": improved,
                    "critique": critique[:200],
                }
            },
        )
        if improved:
            current, current_score, confidence = revised, revised_score, revised_conf
        # A regression is kept in the trace (above) but not adopted — see the
        # module docstring's regression guard.

    return (
        current,
        current_score,
        sets,
        confidence,
        tokens_in,
        tokens_out,
        llm_calls,
        cache_hits,
    )


__all__ = ["FieldOutcome", "select_best", "refine_field", "render_subgraph"]
