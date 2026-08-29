"""The echo NLP worker: consumes stage.nlp, which carries both STAGE_REDACT
and STAGE_NLP envelopes (architecture.md §1.2 gives nlp-service ownership of
redaction; §2.1 defines only two request streams, so redaction rides the
nlp stream rather than a third one the contract doesn't define). Neither
handler does real work yet — Phase 6/7 replace their bodies; the dispatch,
artifact keys, and result contract below are what those phases build on.
"""

from __future__ import annotations

import asyncio
import logging
import time

from google.protobuf import json_format

from coda.v1 import clinical_pb2, common_pb2, transcript_pb2
from coda_worker_sdk import StageContext, StageOutput
from coda_worker_sdk.errors import FatalError

logger = logging.getLogger(__name__)

_ECHO_SLEEP_SECONDS = 1.0


async def handle_nlp_stage(ctx: StageContext) -> StageOutput:
    """Dispatches by envelope.stage — the one nlp-service handler that
    StageWorker calls, since both stages arrive on the same stream/group.
    """
    stage = ctx.envelope.stage
    if stage == common_pb2.Stage.STAGE_REDACT:
        return await _handle_redact(ctx)
    if stage == common_pb2.Stage.STAGE_NLP:
        return await _handle_nlp(ctx)
    raise FatalError(
        f"nlp-service cannot handle stage {stage} "
        "(architecture.md §1.2: asr-service owns STAGE_ASR)",
        code="UNSUPPORTED_STAGE",
    )


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


async def _handle_nlp(ctx: StageContext) -> StageOutput:
    env = ctx.envelope
    started = time.monotonic()
    ctx.logger.info("nlp echo: starting", extra={"extra_fields": {"job_id": env.job_id}})

    ctx.heartbeat.update(percent=15, step="thought construction (echo, no-op)")
    await asyncio.sleep(_ECHO_SLEEP_SECONDS / 3)
    ctx.heartbeat.update(percent=50, step="candidate generation (echo, no-op)")
    await asyncio.sleep(_ECHO_SLEEP_SECONDS / 3)
    ctx.heartbeat.update(percent=85, step="distillation (echo, no-op)")
    await asyncio.sleep(_ECHO_SLEEP_SECONDS / 3)

    note = clinical_pb2.ClinicalNote(
        consultation_id=env.consultation_id,
        run_config_id=env.run_config_id,
        version=1,
        status=clinical_pb2.NoteStatus.NOTE_STATUS_DRAFT,
        chief_complaint=clinical_pb2.FieldValue(value="", source_turn_ids=[], confidence=0.0),
    )
    payload = json_format.MessageToJson(
        note, preserving_proto_field_name=True, indent=None
    ).encode()
    key = ctx.storage.artifact_key(
        env.consultation_id, "nlp", env.run_config_id, "clinical_note", "json"
    )
    await ctx.storage.put_bytes(key, payload, content_type="application/json")

    ctx.heartbeat.update(percent=100, step="done (echo)")
    wall_ms = int((time.monotonic() - started) * 1000)
    ctx.logger.info(
        "nlp echo: done", extra={"extra_fields": {"job_id": env.job_id, "result_ref": key}}
    )
    return StageOutput(
        result_ref=key,
        metrics=common_pb2.StageMetrics(
            tokens_in=0, tokens_out=0, llm_calls=0, wall_ms=wall_ms, model_ids=["echo"]
        ),
    )


__all__ = ["handle_nlp_stage"]
