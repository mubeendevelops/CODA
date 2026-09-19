"""The nlp-service worker: consumes stage.nlp, which carries both
STAGE_REDACT and STAGE_NLP envelopes (architecture.md §1.2 gives nlp-service
ownership of redaction; §2.1 defines only two request streams, so redaction
rides the nlp stream rather than a third one the contract doesn't define).

STAGE_REDACT is still an echo (real PII detection is separate, unbuilt
work — not this task's job). STAGE_NLP runs real single-pass extraction
(Phase 4, claude_context.md §6/§7 decision #66) when the job's `RunConfig`
has `got_enabled = false`.

For `got_enabled = true` the GoT arm runs the full GoT-HCS pipeline:
**Modules 1-2** (thought construction and thought-graph assembly,
`nlp_service.graph`) followed by **Modules 5-6** (candidate generation,
scoring, K-iteration refinement, hierarchical distillation,
`nlp_service.reasoning`). Both arms end in the same place — `extractions`,
`summaries` and `clinical_notes` rows plus a `clinical_note` artifact — so
the ablation compares two routes to one output shape, not two output shapes.

Every GoT stage switch is a `RunConfig` field, never a code branch here
(architecture.md §6): `n_candidates`, `k_iterations`, `graph_context_enabled`,
`scorer_backend`, `scorer_weights`, `generation_context_tokens`.
"""

from __future__ import annotations

import asyncio
import logging
import time

from google.protobuf import json_format

from coda.v1 import clinical_pb2, common_pb2, runconfig_pb2, transcript_pb2
from coda_worker_sdk import PostgresPool, StageContext, StageHandler, StageOutput
from coda_worker_sdk.errors import FatalError
from nlp_service import db, prompts
from nlp_service.extraction import run_extraction
from nlp_service.graph.pipeline import build_thought_graph
from nlp_service.llm.client import LLMClient
from nlp_service.reasoning.pipeline import run_reasoning
from nlp_service.summary import run_summary
from nlp_service.transcript_text import known_turn_ids, render_turns

logger = logging.getLogger(__name__)

_ECHO_SLEEP_SECONDS = 1.0


def build_nlp_handler(
    *, pg_pool: PostgresPool, llm_client: LLMClient, repair_max_attempts: int, timeout_s: float
) -> StageHandler:
    async def handle_nlp_stage(ctx: StageContext) -> StageOutput:
        """Dispatches by envelope.stage — the one nlp-service handler that
        StageWorker calls, since both stages arrive on the same stream/group.
        """
        stage = ctx.envelope.stage
        if stage == common_pb2.Stage.STAGE_REDACT:
            return await _handle_redact(ctx)
        if stage == common_pb2.Stage.STAGE_NLP:
            return await _handle_nlp(
                ctx,
                pg_pool=pg_pool,
                llm_client=llm_client,
                repair_max_attempts=repair_max_attempts,
                timeout_s=timeout_s,
            )
        raise FatalError(
            f"nlp-service cannot handle stage {stage} "
            "(architecture.md §1.2: asr-service owns STAGE_ASR)",
            code="UNSUPPORTED_STAGE",
        )

    return handle_nlp_stage


async def _handle_redact(ctx: StageContext) -> StageOutput:
    env = ctx.envelope
    started = time.monotonic()
    ctx.logger.info("redact echo: starting", extra={"extra_fields": {"job_id": env.job_id}})

    ctx.heartbeat.update(percent=20, step="reading transcript (echo)")
    transcript = transcript_pb2.Transcript()
    if env.payload_ref:
        try:
            raw = await ctx.storage.get_bytes(env.payload_ref)
            json_format.Parse(raw, transcript, ignore_unknown_fields=True)
        except Exception:
            # Best-effort: the echo worker does not depend on real ASR
            # output existing, but reads it when present so the redaction
            # step genuinely operates on the prior stage's artifact rather
            # than a completely disconnected stub.
            ctx.logger.warning(
                "redact echo: could not read input transcript, proceeding with an empty one",
                extra={"extra_fields": {"job_id": env.job_id, "payload_ref": env.payload_ref}},
            )

    await asyncio.sleep(_ECHO_SLEEP_SECONDS / 2)
    ctx.heartbeat.update(percent=70, step="redacting (echo, no-op)")

    # No real PII detection: text_redacted is set to text unchanged. Real
    # pattern/NER-based redaction (architecture.md §7.3) replaces this loop.
    for turn in transcript.turns:
        turn.text_redacted = turn.text
    await asyncio.sleep(_ECHO_SLEEP_SECONDS / 2)

    payload = json_format.MessageToJson(
        transcript, preserving_proto_field_name=True, indent=None
    ).encode()
    key = ctx.storage.artifact_key(
        env.consultation_id, "redact", env.run_config_id, "transcript", "json"
    )
    await ctx.storage.put_bytes(key, payload, content_type="application/json")

    ctx.heartbeat.update(percent=100, step="done (echo)")
    wall_ms = int((time.monotonic() - started) * 1000)
    ctx.logger.info(
        "redact echo: done", extra={"extra_fields": {"job_id": env.job_id, "result_ref": key}}
    )
    return StageOutput(
        result_ref=key,
        metrics=common_pb2.StageMetrics(
            tokens_in=0, tokens_out=0, llm_calls=0, wall_ms=wall_ms, model_ids=["echo"]
        ),
    )


async def _handle_nlp(
    ctx: StageContext,
    *,
    pg_pool: PostgresPool,
    llm_client: LLMClient,
    repair_max_attempts: int,
    timeout_s: float,
) -> StageOutput:
    env = ctx.envelope
    started = time.monotonic()
    ctx.logger.info("nlp: starting", extra={"extra_fields": {"job_id": env.job_id}})

    ctx.heartbeat.update(percent=5, step="loading run config")
    async with pg_pool.connection() as conn:
        rc_row = await db.fetch_run_config(conn, env.run_config_id)
    rc = rc_row.config

    if not rc.base_model:
        raise FatalError(
            f"run_config {env.run_config_id!r} has no base_model set", code="MISSING_BASE_MODEL"
        )

    if not env.payload_ref:
        raise FatalError("STAGE_NLP envelope carries no payload_ref", code="MISSING_PAYLOAD")

    ctx.heartbeat.update(percent=10, step="reading redacted transcript")
    raw = await ctx.storage.get_bytes(env.payload_ref)
    transcript = transcript_pb2.Transcript()
    json_format.Parse(raw, transcript, ignore_unknown_fields=True)

    turns_text = render_turns(transcript)
    turn_ids = known_turn_ids(transcript)
    language = transcript.language or prompts.DEFAULT_LANGUAGE

    if rc.got_enabled:
        return await _handle_got(
            ctx,
            pg_pool=pg_pool,
            llm_client=llm_client,
            run_config=rc,
            arm=rc_row.arm,
            transcript=transcript,
            language=language,
            repair_max_attempts=repair_max_attempts,
            timeout_s=timeout_s,
            started=started,
        )

    ctx.heartbeat.update(percent=25, step="extracting 8-field note")
    async with pg_pool.connection() as conn:
        extraction = await run_extraction(
            conn=conn,
            llm_client=llm_client,
            model=rc.base_model,
            transcript_turns_text=turns_text,
            known_turn_ids=turn_ids,
            consultation_id=env.consultation_id,
            run_config_id=env.run_config_id,
            repair_max_attempts=repair_max_attempts,
            timeout_s=timeout_s,
            language=language,
        )

    ctx.heartbeat.update(percent=65, step="generating consultation summary")
    async with pg_pool.connection() as conn:
        summary = await run_summary(
            conn=conn,
            llm_client=llm_client,
            model=rc.base_model,
            transcript_turns_text=turns_text,
            min_chars=20,
            timeout_s=timeout_s,
            language=language,
        )

    ctx.heartbeat.update(percent=85, step="persisting extraction/summary/note rows")
    async with pg_pool.connection() as conn:
        await db.write_pipeline_outputs(
            conn,
            consultation_id=env.consultation_id,
            run_config_id=env.run_config_id,
            note=extraction.note,
            summary_text=summary.text,
        )

    ctx.heartbeat.update(percent=95, step="writing artifacts")
    note_key = ctx.storage.artifact_key(
        env.consultation_id, "nlp", env.run_config_id, "clinical_note", "json"
    )
    await ctx.storage.put_bytes(
        note_key,
        json_format.MessageToJson(
            extraction.note, preserving_proto_field_name=True, indent=None
        ).encode(),
        content_type="application/json",
    )
    summary_msg = clinical_pb2.Summary(
        consultation_id=env.consultation_id, run_config_id=env.run_config_id, text=summary.text
    )
    summary_key = ctx.storage.artifact_key(
        env.consultation_id, "nlp", env.run_config_id, "summary", "json"
    )
    await ctx.storage.put_bytes(
        summary_key,
        json_format.MessageToJson(
            summary_msg, preserving_proto_field_name=True, indent=None
        ).encode(),
        content_type="application/json",
    )

    wall_ms = int((time.monotonic() - started) * 1000)
    ctx.heartbeat.update(percent=100, step="done")
    # prompt_set_hash: the ACTUAL prompt version used, computed from the
    # files nlp-service loaded — independent of run_config.prompt_set_hash,
    # which go-api currently hardcodes to "" (jobs_handlers.go, no prompt
    # files existed when that code was written). Logged rather than
    # persisted per-row: extractions/summaries/clinical_notes carry no such
    # column today, and adding one is a migration this pass didn't do — see
    # claude_context.md decision #70.
    ctx.logger.info(
        "nlp: done",
        extra={
            "extra_fields": {
                "job_id": env.job_id,
                "result_ref": note_key,
                "wall_ms": wall_ms,
                "schema_valid": extraction.schema_valid,
                "repair_attempts": extraction.repair_attempts,
                "prompt_set_hash": prompts.prompt_set_hash(language=language),
            }
        },
    )

    return StageOutput(
        result_ref=note_key,
        metrics=common_pb2.StageMetrics(
            tokens_in=extraction.tokens_in + summary.tokens_in,
            tokens_out=extraction.tokens_out + summary.tokens_out,
            llm_calls=extraction.llm_calls + summary.llm_calls,
            cache_hits=extraction.cache_hits + summary.cache_hits,
            wall_ms=wall_ms,
            model_ids=[rc.base_model],
            cost_estimate=0.0,
            schema_valid=extraction.schema_valid,
            repair_attempts=extraction.repair_attempts,
        ),
    )


async def _handle_got(
    ctx: StageContext,
    *,
    pg_pool: PostgresPool,
    llm_client: LLMClient,
    run_config: runconfig_pb2.RunConfig,
    arm: str,
    transcript: transcript_pb2.Transcript,
    language: str,
    repair_max_attempts: int,
    timeout_s: float,
    started: float,
) -> StageOutput:
    """The GoT arm: build the thought graph (Modules 1-2), then reason over it
    (Modules 5-6) into the same 8-field note the baseline arm produces.
    """
    env = ctx.envelope
    structural_model = run_config.structural_model or run_config.base_model
    if not run_config.structural_model:
        ctx.logger.warning(
            "run_config has no structural_model; falling back to base_model for edge "
            "prediction, which collapses the quota-bucket split (claude_context.md §8)",
            extra={"extra_fields": {"job_id": env.job_id, "arm": arm}},
        )

    ctx.heartbeat.update(percent=15, step="constructing thoughts and assembling graph")
    async with pg_pool.connection() as conn:
        build = await build_thought_graph(
            conn=conn,
            storage=ctx.storage,
            llm_client=llm_client,
            transcript=transcript,
            consultation_id=env.consultation_id,
            run_config_id=env.run_config_id,
            transcript_uri=env.payload_ref,
            base_model=run_config.base_model,
            structural_model=structural_model,
            asr_backend=run_config.asr_backend or "faster_whisper_local",
            asr_model=run_config.asr_model or "unknown",
            repair_max_attempts=repair_max_attempts,
            timeout_s=timeout_s,
            language=language,
        )

    ctx.logger.info(
        "got: thought graph built",
        extra={
            "extra_fields": {
                "job_id": env.job_id,
                "arm": arm,
                "artifact_key": build.artifact_key,
                "thoughts": len(build.graph.thoughts),
                "edges": len(build.graph.edges),
                "rule_edges": build.rule_edge_count,
                "llm_edges": build.llm_edge_count,
            }
        },
    )

    async with pg_pool.connection() as conn:
        reasoning = await run_reasoning(
            conn=conn,
            storage=ctx.storage,
            llm_client=llm_client,
            graph=build.graph,
            run_config=run_config,
            consultation_id=env.consultation_id,
            run_config_id=env.run_config_id,
            repair_max_attempts=repair_max_attempts,
            timeout_s=timeout_s,
            language=language,
            heartbeat=ctx.heartbeat,
        )

    wall_ms = int((time.monotonic() - started) * 1000)
    ctx.heartbeat.update(percent=100, step="done")
    ctx.logger.info(
        "got: done",
        extra={
            "extra_fields": {
                "job_id": env.job_id,
                "arm": arm,
                "result_ref": reasoning.note_artifact_key,
                "graph_artifact": build.artifact_key,
                "trace_artifacts": list(reasoning.trace_uris.values()),
                "wall_ms": wall_ms,
                "prompt_set_hash": prompts.prompt_set_hash(language=language),
                # The GoT-vs-single-pass cost comparison (plan.md Phase 6 AC5)
                # is assembled from these: graph build and reasoning are
                # reported separately so the ablation can attribute cost to
                # the stage that incurred it.
                "graph_tokens_in": build.tokens_in,
                "graph_tokens_out": build.tokens_out,
                "reasoning_tokens_in": reasoning.tokens_in,
                "reasoning_tokens_out": reasoning.tokens_out,
                "per_field_tokens_apportioned": reasoning.per_field_tokens,
                "failed_variants": reasoning.failed_variants,
            }
        },
    )
    return StageOutput(
        result_ref=reasoning.note_artifact_key,
        metrics=common_pb2.StageMetrics(
            tokens_in=build.tokens_in + reasoning.tokens_in,
            tokens_out=build.tokens_out + reasoning.tokens_out,
            llm_calls=build.llm_calls + reasoning.llm_calls,
            cache_hits=build.cache_hits + reasoning.cache_hits,
            wall_ms=wall_ms,
            model_ids=[run_config.base_model, structural_model, run_config.judge_model or ""],
            cost_estimate=0.0,
            schema_valid=True,
            repair_attempts=build.repair_attempts + reasoning.repair_attempts,
        ),
    )


__all__ = ["build_nlp_handler"]
