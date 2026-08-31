"""The nlp-service worker: consumes stage.nlp, which carries both
STAGE_REDACT and STAGE_NLP envelopes (architecture.md §1.2 gives nlp-service
ownership of redaction; §2.1 defines only two request streams, so redaction
rides the nlp stream rather than a third one the contract doesn't define).

STAGE_REDACT is still an echo (real PII detection is separate, unbuilt
work — not this task's job). STAGE_NLP now runs real single-pass extraction
(Phase 4, claude_context.md §6/§7 decision #66) when the job's `RunConfig`
has `got_enabled = false`; the GoT arm (`got_enabled = true`) is Phase 6,
not built yet, and is rejected with a FatalError rather than silently
echoed.
"""

from __future__ import annotations

import asyncio
import logging
import time

from google.protobuf import json_format

from coda.v1 import clinical_pb2, common_pb2, transcript_pb2
from coda_worker_sdk import PostgresPool, StageContext, StageHandler, StageOutput
from coda_worker_sdk.errors import FatalError
from nlp_service import db, prompts
from nlp_service.extraction import run_extraction
from nlp_service.llm.client import LLMClient
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

    if rc.got_enabled:
        raise FatalError(
            f"GoT arm ({rc_row.arm!r}) is not implemented yet — Phase 6",
            code="GOT_NOT_IMPLEMENTED",
        )
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


__all__ = ["build_nlp_handler"]
