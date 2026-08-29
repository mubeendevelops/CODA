"""The echo ASR worker: consumes one StageEnvelope from stage.asr, does no
real transcription, and returns a valid result. This is Phase 3's walking
skeleton (claude_context.md decision-in-progress, see plan.md) — it proves
the Redis Streams / MinIO / orchestrator plumbing end to end *before* real
Groq/pyannote transcription lands (plan.md Phase 1).

Real transcription replaces `_run` without touching anything else: the
StageWorker wiring in server.py, the artifact key, and the result contract
all stay as they are.
"""

from __future__ import annotations

import asyncio
import logging
import time

from google.protobuf import json_format

from coda.v1 import common_pb2, transcript_pb2
from coda_worker_sdk import StageContext, StageOutput
from coda_worker_sdk.errors import FatalError

logger = logging.getLogger(__name__)

_ECHO_SLEEP_SECONDS = 1.5
"""Brief, not instant — long enough that the heartbeat and progress-polling
path is actually exercised by the e2e smoke test rather than racing past it."""


async def handle_asr_stage(ctx: StageContext) -> StageOutput:
    env = ctx.envelope
    if env.stage != common_pb2.Stage.STAGE_ASR:
        raise FatalError(
            f"asr-service cannot handle stage {env.stage} (architecture.md §1.2: asr-service only)",
            code="UNSUPPORTED_STAGE",
        )

    started = time.monotonic()
    ctx.logger.info(
        "asr echo: starting",
        extra={"extra_fields": {"job_id": env.job_id, "payload_ref": env.payload_ref}},
    )

    ctx.heartbeat.update(percent=10, step="loading audio (echo)")
    await asyncio.sleep(_ECHO_SLEEP_SECONDS / 2)

    ctx.heartbeat.update(percent=60, step="transcribing (echo)")
    await asyncio.sleep(_ECHO_SLEEP_SECONDS / 2)

    transcript = transcript_pb2.Transcript(
        consultation_id=env.consultation_id,
        run_config_id=env.run_config_id,
        language="en",
        asr_backend="echo",
        asr_model="echo-worker-v0",
        turns=[
            transcript_pb2.Turn(
                turn_index=0,
                speaker_label=transcript_pb2.SpeakerRole.SPEAKER_ROLE_DOCTOR,
                start_ms=0,
                end_ms=1000,
                text="[echo worker] no real transcription performed.",
                confidence=1.0,
            )
        ],
    )
    payload = json_format.MessageToJson(
        transcript, preserving_proto_field_name=True, indent=None
    ).encode()

    key = ctx.storage.artifact_key(
        env.consultation_id, "asr", env.run_config_id, "transcript", "json"
    )
    await ctx.storage.put_bytes(key, payload, content_type="application/json")

    ctx.heartbeat.update(percent=100, step="done (echo)")
    wall_ms = int((time.monotonic() - started) * 1000)
    ctx.logger.info(
        "asr echo: done", extra={"extra_fields": {"job_id": env.job_id, "result_ref": key}}
    )

    return StageOutput(
        result_ref=key,
        metrics=common_pb2.StageMetrics(
            tokens_in=0, tokens_out=0, llm_calls=0, wall_ms=wall_ms, model_ids=["echo"]
        ),
    )


__all__ = ["handle_asr_stage"]
