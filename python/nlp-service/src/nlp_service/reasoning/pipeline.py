"""Modules 5 and 6 end to end: thought graph in, distilled 8-field note plus
summary out, with every candidate, score and iteration persisted.

## Ablation is configuration, not code branches

Every stage switch here reads from `RunConfig` and nothing reads an
environment variable or a compiled constant that a run cannot record
(architecture.md §6, ADR-0012). The arms in §6.2's matrix differ only in the
values below:

| Knob | Off | On |
|---|---|---|
| `n_candidates` | 1 — single candidate, scoring cannot change the outcome | N variants |
| `k_iterations` | 0 — no refinement pass runs | K critique/regenerate passes |
| `graph_context_enabled` | every field sees every thought | per-field subgraph retrieval |
| `scorer_backend` | `heuristic` (free) | `llm_judge`, or `both` to record each |
| `scorer_weights` | recorded defaults | any weighting |
| `generation_context_tokens` | compiled default | any per-field budget |

`got_k2_nograph` is a genuine control precisely because turning the graph off
changes only which thoughts reach the prompt — generation, scoring,
refinement and distillation run identical code either way, so a difference in
the result is attributable to retrieval and nothing else.

## Cost accounting

Token and call counts are tracked **per field**, not just per stage, because
the inference-cost comparison against the single-pass baseline is part of the
evaluation (plan.md Phase 6 AC5). Generation is batched across fields, so its
cost is divided evenly among the fields in the batch — an apportionment, not
a measurement, and labelled as such wherever it surfaces. Scoring and
refinement are genuinely per-field and are attributed exactly.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

import psycopg
from google.protobuf import json_format

from coda.v1 import clinical_pb2, got_pb2, runconfig_pb2, thought_pb2
from coda_worker_sdk.errors import FatalError
from nlp_service import prompts
from nlp_service.llm.client import LLMClient
from nlp_service.reasoning import distill, generation, refine, scoring, store
from nlp_service.reasoning.embeddings import resolve_backend

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReasoningResult:
    note: clinical_pb2.ClinicalNote
    summary_text: str
    outcomes: dict[str, refine.FieldOutcome]
    trace_uris: dict[str, str]
    note_artifact_key: str
    summary_artifact_key: str
    tokens_in: int
    tokens_out: int
    llm_calls: int
    cache_hits: int
    repair_attempts: int
    wall_ms: int
    failed_variants: list[str] = field(default_factory=list)
    per_field_tokens: dict[str, int] = field(default_factory=dict)


def resolve_stage_config(rc: runconfig_pb2.RunConfig) -> tuple[int, int, str, int]:
    """`(n_candidates, k_iterations, scorer_backend, generation_context_tokens)`
    with the recorded defaults applied to unset proto3 scalars.

    N defaults to 3 and K to 2 (claude_context.md §6, architecture.md §6.2's
    matrix). `k_iterations = 0` is a legitimate value meaning "no refinement",
    so it is NOT defaulted — but proto3 cannot distinguish it from unset, and
    the ablation matrix has no arm with K=0 for a GoT run, so an unset K
    taking the default is the safer reading. An arm wanting K=0 gets it by
    setting `got_enabled` with `n_candidates` and living with generation-only
    behaviour, which `got_k1` already covers at K=1.
    """
    n = rc.n_candidates or 3
    k = rc.k_iterations or 2
    backend = rc.scorer_backend or scoring.SCORER_HEURISTIC
    if backend not in scoring.VALID_SCORER_BACKENDS:
        raise FatalError(
            f"run_config.scorer_backend = {backend!r} is not one of "
            f"{list(scoring.VALID_SCORER_BACKENDS)}",
            code="INVALID_SCORER_BACKEND",
        )
    ctx_tokens = rc.generation_context_tokens or generation.DEFAULT_GENERATION_CONTEXT_TOKENS
    return n, k, backend, ctx_tokens


async def run_reasoning(
    *,
    conn: psycopg.AsyncConnection,
    storage: object,
    llm_client: LLMClient,
    graph: thought_pb2.ThoughtGraph,
    run_config: runconfig_pb2.RunConfig,
    consultation_id: str,
    run_config_id: str,
    repair_max_attempts: int,
    timeout_s: float,
    language: str = prompts.DEFAULT_LANGUAGE,
    heartbeat: object | None = None,
) -> ReasoningResult:
    started = time.monotonic()
    thoughts = list(graph.thoughts)
    edges = list(graph.edges)
    if not thoughts:
        raise FatalError(
            "reasoning received a thought graph with no thoughts", code="EMPTY_GRAPH"
        )

    n, k, scorer_backend, ctx_tokens = resolve_stage_config(run_config)
    weights = scoring.resolve_weights(run_config)
    embed_backend = resolve_backend(run_config.embed_model)
    field_keys = list(distill.FIELD_KEYS)
    base_model = run_config.base_model
    judge_model = run_config.judge_model or base_model

    def beat(percent: float, step: str) -> None:
        if heartbeat is not None:
            heartbeat.update(percent=percent, step=step)  # type: ignore[attr-defined]

    # ---------------------------------------------------------------- 1. generate
    beat(30, f"generating {n} candidate(s) per field")
    gen = await generation.generate_candidates(
        conn=conn,
        llm_client=llm_client,
        model=base_model,
        field_keys=field_keys,
        thoughts=thoughts,
        edges=edges,
        n_candidates=n,
        graph_context_enabled=run_config.graph_context_enabled,
        context_tokens=ctx_tokens,
        temperature=run_config.temperature,
        repair_max_attempts=repair_max_attempts,
        timeout_s=timeout_s,
        language=language,
    )

    tokens_in, tokens_out = gen.tokens_in, gen.tokens_out
    llm_calls, cache_hits = gen.llm_calls, gen.cache_hits
    repair_attempts = gen.repair_attempts

    # Generation is one batched call per variant covering every field, so its
    # cost cannot be measured per field — only apportioned. Divided evenly and
    # labelled as an apportionment wherever it is reported.
    n_fields = max(1, len(field_keys))
    gen_tokens_in_per_field = gen.tokens_in // n_fields
    gen_tokens_out_per_field = gen.tokens_out // n_fields
    gen_calls_per_field = gen.llm_calls / n_fields

    # ------------------------------------------------------- 2-3. score, select, refine
    outcomes: dict[str, refine.FieldOutcome] = {}
    already_selected: list[str] = []

    for position, field_key in enumerate(field_keys):
        candidates = gen.candidates_by_field.get(field_key, [])
        if not candidates:
            continue
        beat(
            30 + 45 * position / n_fields,
            f"scoring and refining {field_key} ({position + 1}/{n_fields})",
        )

        # The field's supporting thoughts are the union across variants: a
        # candidate must be scored against the evidence IT saw, and different
        # variants saw different subgraphs.
        ctx_thought_ids: set[str] = set()
        context_tokens_used = 0
        for fc in gen.contexts_by_variant:
            rc_ctx = fc.contexts.get(field_key)
            if rc_ctx is not None:
                ctx_thought_ids |= {t.id for t in rc_ctx.thoughts}
                context_tokens_used = max(context_tokens_used, rc_ctx.estimated_tokens)
        by_id = {t.id: t for t in thoughts}
        field_thoughts = [by_id[i] for i in sorted(ctx_thought_ids) if i in by_id]
        field_thoughts.sort(key=lambda t: (t.turn_index, t.char_start, t.id))

        ctx = scoring.ScoringContext(
            field_key=field_key,
            thoughts=field_thoughts,
            already_selected=list(already_selected),
        )

        field_tokens_in = field_tokens_out = 0
        field_calls = field_cache_hits = 0

        heuristic_scores = [
            scoring.score_heuristic(c, ctx, weights=weights, backend=embed_backend)
            for c in candidates
        ]
        judge_scores: list[got_pb2.ScoreBreakdown] = []
        if scorer_backend in (scoring.SCORER_LLM_JUDGE, scoring.SCORER_BOTH):
            judge_scores, j_in, j_out, j_calls, j_hits = await scoring.score_llm_judge(
                candidates,
                ctx,
                conn=conn,
                llm_client=llm_client,
                judge_model=judge_model,
                weights=weights,
                timeout_s=timeout_s,
                language=language,
            )
            tokens_in += j_in
            tokens_out += j_out
            llm_calls += j_calls
            cache_hits += j_hits
            field_tokens_in += j_in
            field_tokens_out += j_out
            field_calls += j_calls
            field_cache_hits += j_hits

        # Which backend drives selection, and which is merely recorded.
        if scorer_backend == scoring.SCORER_LLM_JUDGE and judge_scores:
            primary, secondary = judge_scores, heuristic_scores
        elif scorer_backend == scoring.SCORER_BOTH and judge_scores:
            # `both` selects on the JUDGE and records the heuristic alongside:
            # if we are paying for the judge, it should be the one deciding,
            # and the free scorer's value here is the agreement measurement.
            primary, secondary = judge_scores, heuristic_scores
        else:
            primary, secondary = heuristic_scores, []

        chosen = refine.select_best(candidates, primary)
        selected, selected_score = candidates[chosen], primary[chosen]
        trajectory = [selected_score.aggregate]

        iteration_sets = [
            got_pb2.CandidateSet(
                consultation_id=consultation_id,
                run_config_id=run_config_id,
                iteration=0,
                candidates=candidates,
                scores=primary,
                secondary_scores=secondary,
                selected_index=chosen,
                context_tokens=context_tokens_used,
            )
        ]

        if k > 0:
            (
                selected,
                selected_score,
                refine_sets,
                confidence,
                r_in,
                r_out,
                r_calls,
                r_hits,
            ) = await refine.refine_field(
                conn=conn,
                llm_client=llm_client,
                model=base_model,
                field_key=field_key,
                thoughts=field_thoughts,
                candidate=selected,
                score=selected_score,
                ctx=ctx,
                weights=weights,
                embed_backend=embed_backend,
                k_iterations=k,
                temperature=run_config.temperature,
                repair_max_attempts=repair_max_attempts,
                timeout_s=timeout_s,
                language=language,
            )
            tokens_in += r_in
            tokens_out += r_out
            llm_calls += r_calls
            cache_hits += r_hits
            field_tokens_in += r_in
            field_tokens_out += r_out
            field_calls += r_calls
            field_cache_hits += r_hits
            for cs in refine_sets:
                cs.consultation_id = consultation_id
                cs.run_config_id = run_config_id
                iteration_sets.append(cs)
                trajectory.append(cs.scores[0].aggregate)
        else:
            confidence = 0.0

        outcome = refine.FieldOutcome(
            field_key=field_key,
            trace=got_pb2.RefinementTrace(),  # filled in after distillation
            selected=selected,
            score=selected_score,
            secondary_score=secondary[chosen] if secondary else None,
            source_thought_ids=list(selected.source_thought_ids),
            confidence=confidence or _confidence_floor(selected_score),
            tokens_in=field_tokens_in + gen_tokens_in_per_field,
            tokens_out=field_tokens_out + gen_tokens_out_per_field,
            llm_calls=field_calls + round(gen_calls_per_field),
            cache_hits=field_cache_hits,
        )
        outcome.trace = store.build_trace(
            consultation_id=consultation_id,
            run_config_id=run_config_id,
            field_key=field_key,
            iterations=iteration_sets,
            final_value=clinical_pb2.FieldValue(),
            trajectory=trajectory,
            scorer_backend=scorer_backend,
            tokens_in=outcome.tokens_in,
            tokens_out=outcome.tokens_out,
            llm_calls=outcome.llm_calls,
            cache_hits=outcome.cache_hits,
        )
        outcomes[field_key] = outcome

        if selected.text.strip():
            already_selected.append(selected.text)

    if not outcomes:
        raise FatalError(
            "no field produced a candidate; there is nothing to distil into a note",
            code="NO_FIELD_OUTCOMES",
        )

    # ---------------------------------------------------------------- 4. distil
    beat(80, "distilling the 8-field note")
    note = distill.distill_note(
        outcomes,
        thoughts=thoughts,
        consultation_id=consultation_id,
        run_config_id=run_config_id,
    )

    beat(88, "generating the consultation summary")
    summary = await distill.generate_summary(
        conn=conn,
        llm_client=llm_client,
        model=base_model,
        note=note,
        temperature=run_config.temperature,
        timeout_s=timeout_s,
        language=language,
    )
    tokens_in += summary.tokens_in
    tokens_out += summary.tokens_out
    llm_calls += summary.llm_calls
    cache_hits += summary.cache_hits

    # ------------------------------------------------------------- 5. persist
    beat(92, "writing refinement traces")
    # One artifact for every field's trace, not one per field: the §3.3 key
    # format's `kind` component must be one of common.proto's closed
    # ArtifactKind values (found live 2026-09-05 — `f"trace_{field_key}"`
    # produced an open-ended kind per field, which `build_artifact_key`
    # correctly rejects, and there is no per-field slot elsewhere in the key
    # to put it instead: `stage` describes the pipeline stage, not a content
    # variant, and the `{kind}.{ext}` basename round-trips through exactly
    # one dot in `parse_artifact_key`, so a field name can't safely ride in
    # `ext` either). "candidate_set" is the closed kind this content already
    # matches — one artifact holding every field's `RefinementTrace`, the
    # same way "thought_graph" is one artifact for every thought.
    combined_traces: dict[str, object] = {}
    for field_key, outcome in outcomes.items():
        # The final value is known only after distillation resolved turn ids.
        outcome.trace.final_value.CopyFrom(
            clinical_pb2.FieldValue(
                value=outcome.selected.text,
                source_turn_ids=outcome.turn_ids,
                confidence=outcome.confidence,
            )
        )
        combined_traces[field_key] = json_format.MessageToDict(
            outcome.trace, preserving_proto_field_name=True
        )

    traces_key = storage.artifact_key(  # type: ignore[attr-defined]
        consultation_id, "nlp", run_config_id, "candidate_set", "json"
    )
    await storage.put_bytes(  # type: ignore[attr-defined]
        traces_key,
        json.dumps(combined_traces).encode(),
        content_type="application/json",
    )
    trace_uris: dict[str, str] = {field_key: traces_key for field_key in outcomes}

    await store.write_reasoning_outputs(
        conn,
        consultation_id=consultation_id,
        run_config_id=run_config_id,
        note=note,
        summary_text=summary.text,
        outcomes=outcomes,
        trace_uris=trace_uris,
    )

    note_key = storage.artifact_key(  # type: ignore[attr-defined]
        consultation_id, "nlp", run_config_id, "clinical_note", "json"
    )
    await storage.put_bytes(  # type: ignore[attr-defined]
        note_key, distill.note_to_json(note).encode(), content_type="application/json"
    )
    summary_msg = clinical_pb2.Summary(
        consultation_id=consultation_id, run_config_id=run_config_id, text=summary.text
    )
    summary_key = storage.artifact_key(  # type: ignore[attr-defined]
        consultation_id, "nlp", run_config_id, "summary", "json"
    )
    await storage.put_bytes(  # type: ignore[attr-defined]
        summary_key,
        json_format.MessageToJson(
            summary_msg, preserving_proto_field_name=True, indent=None
        ).encode(),
        content_type="application/json",
    )

    wall_ms = int((time.monotonic() - started) * 1000)
    per_field_tokens = {fk: o.tokens_in + o.tokens_out for fk, o in outcomes.items()}
    logger.info(
        "got reasoning complete",
        extra={
            "extra_fields": {
                "consultation_id": consultation_id,
                "run_config_id": run_config_id,
                "n_candidates": n,
                "k_iterations": k,
                "scorer_backend": scorer_backend,
                "graph_context_enabled": run_config.graph_context_enabled,
                "embed_backend": embed_backend.name,
                "fields_populated": len(outcomes),
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "llm_calls": llm_calls,
                "cache_hits": cache_hits,
                "wall_ms": wall_ms,
                # Per-field accounting for the cost comparison against
                # single-pass. Generation's share is apportioned evenly across
                # fields (one batched call covers all of them), scoring and
                # refinement are exact.
                "per_field_tokens_apportioned": per_field_tokens,
                "score_trajectories": {
                    fk: [round(v, 4) for v in o.trace.score_trajectory]
                    for fk, o in outcomes.items()
                },
            }
        },
    )
    return ReasoningResult(
        note=note,
        summary_text=summary.text,
        outcomes=outcomes,
        trace_uris=trace_uris,
        note_artifact_key=note_key,
        summary_artifact_key=summary_key,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        llm_calls=llm_calls,
        cache_hits=cache_hits,
        repair_attempts=repair_attempts,
        wall_ms=wall_ms,
        failed_variants=gen.failed_variants,
        per_field_tokens=per_field_tokens,
    )


def _confidence_floor(score: got_pb2.ScoreBreakdown) -> float:
    """A K=0 run never asks the model for a confidence (only the refinement
    prompt returns one), so the selected candidate's aggregate stands in.
    Clamped into [0, 1] because the aggregate can go negative when the
    redundancy penalty outweighs the positive terms."""
    return max(0.0, min(1.0, score.aggregate))


__all__ = ["ReasoningResult", "resolve_stage_config", "run_reasoning"]
