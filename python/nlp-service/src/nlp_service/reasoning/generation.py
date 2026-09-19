"""Module 5's candidate generation: N candidates per field, from N genuinely
different views of the thought graph.

## Why the candidates differ by *context*, not just by temperature

The obvious way to get N candidates is to sample one prompt N times at a
non-zero temperature. We do not do that, for two reasons. It makes the run
non-reproducible, which architecture.md §6.3 forbids outright — every result
must be reconstructible from the database, and a sampled candidate set is not.
And it produces variation in wording rather than in reasoning, which tests
nothing about whether the *graph* helps.

Instead each candidate comes from a `GenerationVariant`: a distinct retrieval
policy (a different subgraph) paired with a distinct instruction. Candidate 0
sees a tight, high-priority neighbourhood and is told to stay close to it;
candidate 1 sees a wider neighbourhood and is told to synthesize across it;
candidate 2 sees the tight neighbourhood but is told to prioritize
completeness of pertinent negatives. They disagree because they were shown
different evidence and asked different questions — which is what makes the
scorer's job meaningful, and what lets a case-study figure say *why* two
candidates differ.

Temperature stays at `RunConfig.temperature` (0.0 by default) throughout.

## Batching and the token budget

claude_context.md §8's mitigation #4 originally batched every field into one
call per variant to hold down call count. A real OTPM ceiling on this
model's output tokens/minute (found live 2026-09-05, see
`FIELDS_PER_GENERATION_CALL`'s and `nlp_service.llm.limits`' docstrings)
made that impossible: real `source_thought_ids` are full database UUIDs, and
even a single verbose field's citations could consume the entire safe
`max_tokens` budget, truncating a multi-field response before the rest were
ever written. `FIELDS_PER_GENERATION_CALL` controls how many fields share
one call — currently 1, i.e. one call per field per variant, the batching
mitigation's cost with none of its benefit, kept configurable in case a
future account tier's higher OTPM allowance makes batching viable again
without a code change.

The per-field context budget here is deliberately much smaller than graph
retrieval's own default (`RetrievalPolicy.token_budget`, 900) — retrieval's
budget is sized for a single field in isolation, and a narrower generation
budget does useful work of its own: variants that see less context produce
more genuinely different candidates than variants that all see nearly
everything.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace

import psycopg

from coda.v1 import got_pb2, thought_pb2
from coda_worker_sdk.errors import FatalError
from nlp_service import prompts
from nlp_service.graph.retrieval import (
    RetrievalPolicy,
    RetrievedContext,
    estimate_tokens,
    render_thought_line,
    retrieve_field_context,
)
from nlp_service.llm.client import LLMClient
from nlp_service.llm.limits import SINGLE_FIELD_MAX_TOKENS
from nlp_service.llm_cache import complete_cached
from nlp_service.reasoning import schema as rschema

logger = logging.getLogger(__name__)

DEFAULT_GENERATION_CONTEXT_TOKENS = 300
"""Per-field context budget for a batched generation call. Eight fields at
300 is ~2.4K of context per call; N=3 variants is ~7.2K, against
claude_context.md §8's ~7.5K estimate for this stage (decision #98)."""

## max_tokens history (2026-09-05): see nlp_service.llm.limits' docstring —
## first scaled up per-field to stop hidden reasoning from starving the
## answer, then found that Groq's separate OTPM per-minute ceiling rejects a
## large max_tokens outright; `reasoning_effort: "none"` (client-level) fixed
## the original problem, so this uses OTPM_SAFE_MAX_TOKENS. Confirmed live
## (a 30-minute fully-idle cooldown changed nothing) that OTPM=1000 is a
## real, standing ceiling for this model on this account, not a transient
## burst penalty — so a single call covering every field, even capped at
## OTPM_SAFE_MAX_TOKENS, was still landing short of what 9 fields' worth of
## content needs, failing schema validation on truncated JSON every time.
## First fix attempt split one variant's generation into 3-field chunks —
## still not small enough: a real llm_cache row showed one verbose field's
## value plus 7 full-UUID `source_thought_ids` (each ~10-15 tokens on its
## own — these are real database UUIDs, not the short "t1"-style ids test
## fixtures use) consuming the ENTIRE 900-token budget by itself, truncating
## the JSON before the chunk's other two fields were ever written. One field
## per call is the fix that is actually small enough given real citation
## costs. Even then, a field retrieving many supporting thoughts (one cited
## 19) still needed ~750 real output tokens, so declaring the full
## `OTPM_SAFE_MAX_TOKENS` per call would cap throughput at roughly one field
## per OTPM window regardless — fixed with a source-citation cap (at most 6,
## in both the prompt and `reasoning.schema`'s `maxItems`) plus
## `SINGLE_FIELD_MAX_TOKENS`, a smaller declared ceiling sized to that capped
## worst case so more single-field calls fit per window (see
## nlp_service.llm.limits' docstring point 3).

FIELDS_PER_GENERATION_CALL = 1
"""Each generation call covers this many fields, not all of `field_keys`
(9 in this system) at once — see the max_tokens history note above. Even one
field, with several real (full-UUID) `source_thought_ids` citations, was
observed live to consume the entire OTPM_SAFE_MAX_TOKENS budget; a 3-field
chunk left no room for two of its three fields. Trades more calls per
variant (9x, up from the original 1x) for each call individually fitting
the real, unraisable OTPM ceiling."""


def chunk_fields(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


LIST_FIELDS = frozenset(
    {
        "past_medical_history",
        "medications",
        "allergies",
        "provisional_diagnosis",
        "investigations_advised",
    }
)
"""Fields whose value is a list. Mirrors clinical.proto's ClinicalNote shape
and migration 000019's field_key CHECK; the four not listed are scalar."""


@dataclass(frozen=True, slots=True)
class GenerationVariant:
    name: str
    instruction: str
    """Prepended to the user prompt as `{variant_instruction}`."""
    policy_overrides: dict[str, object] = field(default_factory=dict)
    """Applied to the base RetrievalPolicy, so each variant sees a different
    subgraph."""


VARIANTS: tuple[GenerationVariant, ...] = (
    GenerationVariant(
        name="conservative",
        instruction=(
            "Write conservatively. Include only what the supporting thoughts "
            "state directly. Prefer the patient's or doctor's own phrasing where "
            "it is clinically usable. If two thoughts disagree, record the later, "
            "more authoritative one and note that it corrects an earlier "
            "statement. When in doubt, leave the field empty rather than infer."
        ),
        policy_overrides={"max_depth": 1},
    ),
    GenerationVariant(
        name="inclusive",
        instruction=(
            "Write comprehensively. Synthesize across all the supporting thoughts "
            "for each field into one coherent clinical statement, combining "
            "details that arrived in separate turns — an onset in one thought and "
            "a character in another describe the same finding and belong in one "
            "sentence. Still cite every thought you drew on, and still never "
            "state anything the thoughts do not support."
        ),
        policy_overrides={"max_depth": 2, "min_priority": 0.01},
    ),
    GenerationVariant(
        name="negative_aware",
        instruction=(
            "Write with particular attention to what was ruled out. Pertinent "
            "negatives — symptoms denied, allergies disproved, diagnoses "
            "excluded — are clinically valuable and belong in the note, stated as "
            "negatives. Make sure every `negated` supporting thought is either "
            "reflected in the field it belongs to or genuinely irrelevant to it. "
            "Never state a negated thought's content as though it were true."
        ),
        policy_overrides={"max_depth": 1},
    ),
)
"""The variant pool, in priority order. N=1 uses only `conservative`; N=3 uses
all three; N>3 cycles with a widening depth so extra candidates still differ.

`negative_aware` exists because polarity handling is this project's specific
claim about dialogue (H3, decision #89) and a candidate pool that never
explicitly attends to negation would under-test it."""


def variants_for(n: int) -> list[GenerationVariant]:
    if n <= 0:
        raise ValueError(f"n_candidates must be >= 1, got {n}")
    out = [VARIANTS[i % len(VARIANTS)] for i in range(n)]
    # Beyond the pool, widen each repeat so an N=5 run is five different
    # views rather than three views and two duplicates.
    for i in range(len(VARIANTS), n):
        base = out[i]
        cycle = i // len(VARIANTS)
        overrides = dict(base.policy_overrides)
        current_depth = overrides.get("max_depth", 2)
        depth = (current_depth if isinstance(current_depth, int) else 2) + cycle
        overrides["max_depth"] = depth
        out[i] = replace(
            base, name=f"{base.name}_d{depth}", policy_overrides=overrides
        )
    return out


def _apply(policy: RetrievalPolicy, overrides: dict[str, object]) -> RetrievalPolicy:
    return replace(policy, **overrides)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class FieldContexts:
    """One variant's retrieved subgraph for every requested field."""

    variant: GenerationVariant
    contexts: dict[str, RetrievedContext]

    @property
    def total_tokens(self) -> int:
        return sum(c.estimated_tokens for c in self.contexts.values())


def build_variant_contexts(
    *,
    variant: GenerationVariant,
    field_keys: list[str],
    thoughts: list[thought_pb2.Thought],
    edges: list[thought_pb2.ThoughtEdge],
    context_tokens: int,
) -> FieldContexts:
    base = RetrievalPolicy(token_budget=context_tokens)
    policy = _apply(base, variant.policy_overrides)
    return FieldContexts(
        variant=variant,
        contexts={
            fk: retrieve_field_context(
                field_key=fk, thoughts=thoughts, edges=edges, policy=policy
            )
            for fk in field_keys
        },
    )


def full_transcript_contexts(
    *, field_keys: list[str], thoughts: list[thought_pb2.Thought], variant: GenerationVariant
) -> FieldContexts:
    """The `graph_context_enabled = false` arm: every field sees every
    thought, with no graph traversal at all.

    This is the ablation control that isolates graph-structured retrieval
    (architecture.md §6.2's `got_k2_nograph`). It is deliberately implemented
    as a different *context*, not a different code path — everything
    downstream (generation, scoring, refinement, distillation) is identical,
    so a difference in results is attributable to the retrieval and nothing
    else.
    """
    ordered = sorted(thoughts, key=lambda t: (t.turn_index, t.char_start, t.id))
    tokens = sum(estimate_tokens(render_thought_line(t)) for t in ordered)
    return FieldContexts(
        variant=variant,
        contexts={
            fk: RetrievedContext(
                field_key=fk,
                thoughts=list(ordered),
                priority={t.id: 1.0 for t in ordered},
                seed_ids=tuple(t.id for t in ordered),
                estimated_tokens=tokens,
            )
            for fk in field_keys
        },
    )


def render_field_blocks(contexts: dict[str, RetrievedContext]) -> str:
    blocks = []
    for field_key, ctx in contexts.items():
        shape = "list-valued" if field_key in LIST_FIELDS else "scalar"
        lines = [f"### {field_key} ({shape})"]
        if not ctx.thoughts:
            lines.append(
                "(no supporting thoughts were retrieved for this field — output "
                "an empty value citing nothing)"
            )
        else:
            for t in ctx.thoughts:
                lines.append(f"- {t.id}  {render_thought_line(t)}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


@dataclass(frozen=True, slots=True)
class GenerationResult:
    candidates_by_field: dict[str, list[got_pb2.Candidate]]
    contexts_by_variant: list[FieldContexts]
    tokens_in: int = 0
    tokens_out: int = 0
    llm_calls: int = 0
    cache_hits: int = 0
    repair_attempts: int = 0
    failed_variants: list[str] = field(default_factory=list)
    """Variants whose call never produced valid JSON. Recorded rather than
    raised: N-1 candidates is a degraded pool, not a failed consultation."""


async def generate_candidates(
    *,
    conn: psycopg.AsyncConnection,
    llm_client: LLMClient,
    model: str,
    field_keys: list[str],
    thoughts: list[thought_pb2.Thought],
    edges: list[thought_pb2.ThoughtEdge],
    n_candidates: int,
    graph_context_enabled: bool,
    context_tokens: int,
    temperature: float,
    repair_max_attempts: int,
    timeout_s: float,
    language: str = prompts.DEFAULT_LANGUAGE,
) -> GenerationResult:
    """N batched calls, one per variant, each covering every field."""
    pr = prompts.load_generation_prompts(language=language)
    variants = variants_for(n_candidates)

    candidates_by_field: dict[str, list[got_pb2.Candidate]] = {fk: [] for fk in field_keys}
    all_contexts: list[FieldContexts] = []
    tokens_in = tokens_out = cache_hits = repair_attempts = 0
    llm_calls = 0
    failed: list[str] = []

    for index, variant in enumerate(variants):
        if graph_context_enabled:
            fc = build_variant_contexts(
                variant=variant,
                field_keys=field_keys,
                thoughts=thoughts,
                edges=edges,
                context_tokens=context_tokens,
            )
        else:
            fc = full_transcript_contexts(
                field_keys=field_keys, thoughts=thoughts, variant=variant
            )
        all_contexts.append(fc)

        # Split into several smaller calls rather than one call covering
        # every field — see the max_tokens history note above. A chunk
        # failing its repair budget fails the WHOLE variant (not just that
        # chunk): a variant with only some of its fields populated would
        # need every downstream consumer to handle partial candidates, and
        # "N-1 candidates instead of N" (the existing per-variant failure
        # mode) already degrades gracefully without that complexity.
        merged_fields: dict[str, object] = {}
        variant_failed = False
        last_error = ""

        for chunk_keys in chunk_fields(field_keys, FIELDS_PER_GENERATION_CALL):
            chunk_contexts = {fk: fc.contexts[fk] for fk in chunk_keys}
            known_ids = {fk: {t.id for t in ctx.thoughts} for fk, ctx in chunk_contexts.items()}
            base_user = pr.user_template.format(
                variant_instruction=variant.instruction,
                field_blocks=render_field_blocks(chunk_contexts),
                field_keys=", ".join(chunk_keys),
            )
            user_prompt = base_user
            chunk_parsed: dict[str, object] | None = None

            for attempt in range(repair_max_attempts + 1):
                # Found live 2026-09-05: Groq can refuse the request outright
                # (400, code="GROQ_BAD_REQUEST") rather than returning
                # truncated content when a field's real citations (full
                # database UUIDs, several of them for a field with many
                # "protected" — unconditionally included — supporting
                # thoughts) genuinely cannot fit in SINGLE_FIELD_MAX_TOKENS. This
                # is a "response too long" problem the model can fix by being
                # more concise, not a malformed-request bug, so it is treated
                # as a repairable validation failure (with no
                # `completion.content` to show, since none was returned)
                # rather than left to propagate as fatal and kill the whole
                # variant on the first attempt.
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
                        max_tokens=SINGLE_FIELD_MAX_TOKENS,
                    )
                except FatalError as exc:
                    last_error = (
                        "the response was rejected before returning any content, most likely "
                        f"because it would have exceeded the output token limit ({exc})"
                    )
                    logger.warning(
                        "candidate generation call rejected outright",
                        extra={
                            "extra_fields": {
                                "variant": variant.name,
                                "fields": chunk_keys,
                                "attempt": attempt,
                                "error": last_error,
                            }
                        },
                    )
                    if attempt >= repair_max_attempts:
                        break
                    repair_attempts += 1
                    user_prompt = base_user + pr.repair_addendum_template.format(
                        validation_error=(
                            "Your previous response was too long and was rejected before any "
                            "content was returned. Be extremely concise: state the value in one "
                            "short clause (or the shortest possible list items) and cite only "
                            "the 2-3 most essential supporting thought ids, not all of them."
                        ),
                        previous_response="(no response was returned)",
                    )
                    continue

                tokens_in += completion.tokens_in
                tokens_out += completion.tokens_out
                llm_calls += 1
                if cache_hit:
                    cache_hits += 1

                result = rschema.validate_generation(
                    completion.content, known_thought_ids=known_ids, list_fields=set(LIST_FIELDS)
                )
                if result.valid:
                    chunk_parsed = result.parsed
                    break
                last_error = result.error
                logger.warning(
                    "candidate generation validation failed",
                    extra={
                        "extra_fields": {
                            "variant": variant.name,
                            "fields": chunk_keys,
                            "attempt": attempt,
                            "error": last_error,
                        }
                    },
                )
                if attempt >= repair_max_attempts:
                    break
                repair_attempts += 1
                user_prompt = base_user + pr.repair_addendum_template.format(
                    validation_error=last_error, previous_response=completion.content
                )

            if chunk_parsed is None:
                variant_failed = True
                break
            merged_fields.update(chunk_parsed["fields"])  # type: ignore[arg-type]

        if variant_failed:
            # One variant failing costs one candidate, not the consultation.
            # The pool is only empty if EVERY variant failed, which is checked
            # after the loop.
            failed.append(variant.name)
            logger.error(
                "candidate generation variant exhausted its repair budget",
                extra={"extra_fields": {"variant": variant.name, "last_error": last_error}},
            )
            continue

        for field_key in field_keys:
            payload = merged_fields[field_key]
            assert isinstance(payload, dict)
            items = rschema.str_list(payload, "items")
            text = ", ".join(items) if field_key in LIST_FIELDS else rschema.scalar_text(payload)
            candidates_by_field[field_key].append(
                got_pb2.Candidate(
                    index=index,
                    text=text,
                    items=items,
                    source_thought_ids=rschema.str_list(payload, "source_thought_ids"),
                    generated_by_model=model,
                    variant=variant.name,
                    is_refinement=False,
                )
            )

    if all(not v for v in candidates_by_field.values()):
        raise FatalError(
            f"every one of the {len(variants)} generation variant(s) failed to produce "
            f"valid output; there is nothing to score or refine",
            code="GENERATION_FAILED",
        )

    logger.info(
        "candidate generation complete",
        extra={
            "extra_fields": {
                "variants": [v.name for v in variants],
                "failed_variants": failed,
                "fields": len(field_keys),
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "llm_calls": llm_calls,
                "context_tokens_per_variant": [fc.total_tokens for fc in all_contexts],
            }
        },
    )
    return GenerationResult(
        candidates_by_field=candidates_by_field,
        contexts_by_variant=all_contexts,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        llm_calls=llm_calls,
        cache_hits=cache_hits,
        repair_attempts=repair_attempts,
        failed_variants=failed,
    )


__all__ = [
    "DEFAULT_GENERATION_CONTEXT_TOKENS",
    "FIELDS_PER_GENERATION_CALL",
    "LIST_FIELDS",
    "VARIANTS",
    "GenerationVariant",
    "FieldContexts",
    "GenerationResult",
    "chunk_fields",
    "variants_for",
    "build_variant_contexts",
    "full_transcript_contexts",
    "render_field_blocks",
    "generate_candidates",
]
