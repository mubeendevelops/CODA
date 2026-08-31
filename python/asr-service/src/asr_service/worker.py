"""The real ASR worker: preprocess -> faster-whisper transcription (chunked
for long audio) -> pyannote diarization -> WhisperX-style word/speaker
alignment -> turn assembly -> Groq few-shot Doctor/Patient role assignment
-> assembled Transcript artifact.

Models are loaded once at process startup (asr_service.models, wired in
server.py) and passed in via `build_asr_handler`'s closure — never
constructed per message.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time

from google.protobuf import json_format

from asr_service.align import assign_speakers, build_turns
from asr_service.audio import preprocess
from asr_service.diarize import diarize
from asr_service.models import ModelBundle
from asr_service.roles import assign_roles
from asr_service.transcribe import transcribe
from coda.v1 import common_pb2, transcript_pb2
from coda_worker_sdk import StageContext, StageHandler, StageOutput
from coda_worker_sdk.errors import FatalError

logger = logging.getLogger(__name__)

_ROLE_UNKNOWN = transcript_pb2.SpeakerRole.SPEAKER_ROLE_UNKNOWN


def build_asr_handler(bundle: ModelBundle) -> StageHandler:
    async def handle_asr_stage(ctx: StageContext) -> StageOutput:
        env = ctx.envelope
        if env.stage != common_pb2.Stage.STAGE_ASR:
            raise FatalError(
                f"asr-service cannot handle stage {env.stage} "
                "(architecture.md §1.2: asr-service only)",
                code="UNSUPPORTED_STAGE",
            )
        if not env.payload_ref:
            raise FatalError("STAGE_ASR envelope carries no payload_ref", code="MISSING_PAYLOAD")

        cfg = bundle.cfg
        started = time.monotonic()
        # Per-job override point for the future (RunConfig / envelope wiring
        # not built yet — see config.py's module docstring); config default
        # covers v1, where every consultation is "en" anyway.
        language = env.labels.get("language") or cfg.language

        ctx.logger.info(
            "asr: starting",
            extra={"extra_fields": {"job_id": env.job_id, "payload_ref": env.payload_ref}},
        )

        # STAGE_ASR's payload_ref is the consultation's source audio key
        # (go/internal/storage/key.go SourceAudioKey:
        # {env}/consultations/{id}/source/audio/{sha256}.{ext}), not a §3.3
        # stage-output key — parse_artifact_key doesn't apply here, only the
        # basename extension is needed.
        if "." not in env.payload_ref:
            raise FatalError(
                f"STAGE_ASR payload_ref {env.payload_ref!r} has no extension",
                code="MISSING_PAYLOAD",
            )
        ext = env.payload_ref.rpartition(".")[-1]

        ctx.heartbeat.update(percent=5, step="downloading audio")
        buf = io.BytesIO()
        await ctx.storage.get_stream(env.payload_ref, buf)
        raw = buf.getvalue()

        ctx.heartbeat.update(percent=15, step="preprocessing audio")
        audio = preprocess(
            raw,
            ext=ext,
            target_loudness_dbfs=cfg.target_loudness_dbfs,
            min_duration_s=cfg.min_audio_duration_s,
            max_duration_s=cfg.max_audio_duration_s,
        )

        ctx.heartbeat.update(percent=25, step="transcribing")

        def _on_chunk_done(done: int, total: int) -> None:
            # Runs inside the to_thread call below — heartbeat state is a
            # plain mutable the SDK reads on its own timer, so updating it
            # from a worker thread is safe (no cross-loop scheduling needed).
            pct = 25 + int(35 * done / max(total, 1))
            ctx.heartbeat.update(percent=pct, step=f"transcribing chunk {done}/{total}")

        transcription = await asyncio.to_thread(
            transcribe,
            bundle.whisper,
            audio.samples,
            sample_rate=audio.sample_rate,
            language=language,
            chunk_length_s=cfg.chunk_length_s,
            chunk_overlap_s=cfg.chunk_overlap_s,
            on_chunk_done=_on_chunk_done,
        )

        ctx.heartbeat.update(percent=65, step="diarizing speakers")
        segments = await asyncio.to_thread(
            diarize, bundle.diarizer, audio.samples, audio.sample_rate
        )

        ctx.heartbeat.update(percent=75, step="aligning words to speakers")
        words_with_speaker = assign_speakers(transcription.words, segments)
        turn_drafts = build_turns(words_with_speaker, pause_break_s=cfg.pause_turn_break_s)

        ctx.heartbeat.update(percent=85, step="assigning speaker roles")
        role_assignments, groq_tokens_in, groq_tokens_out = await assign_roles(
            turn_drafts,
            api_key=cfg.groq_api_key,
            model=cfg.role_model,
            confidence_threshold=cfg.role_confidence_threshold,
        )
        for cluster_id, assignment in role_assignments.items():
            if assignment.uncertain:
                ctx.logger.warning(
                    "uncertain speaker role assignment",
                    extra={
                        "extra_fields": {
                            "job_id": env.job_id,
                            "cluster_id": cluster_id,
                            "confidence": assignment.confidence,
                        }
                    },
                )

        turns_proto = []
        for i, draft in enumerate(turn_drafts):
            cluster_id = draft.speaker
            turn_assignment = role_assignments.get(cluster_id)
            speaker_label = turn_assignment.role if turn_assignment is not None else _ROLE_UNKNOWN
            turns_proto.append(
                transcript_pb2.Turn(
                    turn_index=i,
                    speaker_label=speaker_label,
                    start_ms=int(draft.start_s * 1000),
                    end_ms=int(draft.end_s * 1000),
                    text=draft.text,
                    confidence=draft.confidence,
                    words=[
                        transcript_pb2.Word(
                            text=w.text.strip(),
                            start_ms=int(w.start_s * 1000),
                            end_ms=int(w.end_s * 1000),
                            confidence=w.probability,
                            speaker_cluster_id=cluster_id,
                        )
                        for w in draft.words
                    ],
                )
            )

        speaker_clusters_proto = [
            transcript_pb2.SpeakerCluster(
                cluster_id=cluster_id,
                assigned_role=assignment.role,
                confidence=assignment.confidence,
            )
            for cluster_id, assignment in role_assignments.items()
        ]

        transcript = transcript_pb2.Transcript(
            consultation_id=env.consultation_id,
            run_config_id=env.run_config_id,
            language=language,
            asr_backend="faster_whisper_local",
            asr_model=f"{cfg.model_size}/{cfg.compute_type}",
            turns=turns_proto,
            speaker_clusters=speaker_clusters_proto,
        )
        payload = json_format.MessageToJson(
            transcript, preserving_proto_field_name=True, indent=None
        ).encode("utf-8")

        ctx.heartbeat.update(percent=95, step="writing transcript artifact")
        result_key = ctx.storage.artifact_key(
            env.consultation_id, "asr", env.run_config_id, "transcript", "json"
        )
        await ctx.storage.put_bytes(
            result_key, payload, content_type="application/json; charset=utf-8"
        )

        wall_ms = int((time.monotonic() - started) * 1000)
        ctx.heartbeat.update(percent=100, step="done")
        ctx.logger.info(
            "asr: done",
            extra={
                "extra_fields": {
                    "job_id": env.job_id,
                    "result_ref": result_key,
                    "wall_ms": wall_ms,
                    "audio_duration_s": audio.duration_s,
                    "turns": len(turns_proto),
                    "speakers": len(speaker_clusters_proto),
                }
            },
        )

        return StageOutput(
            result_ref=result_key,
            metrics=common_pb2.StageMetrics(
                tokens_in=groq_tokens_in,
                tokens_out=groq_tokens_out,
                llm_calls=1 if role_assignments else 0,
                wall_ms=wall_ms,
                model_ids=[
                    f"faster-whisper:{cfg.model_size}",
                    f"pyannote:{cfg.diarization_model}",
                    f"groq:{cfg.role_model}",
                ],
            ),
        )

    return handle_asr_stage


__all__ = ["build_asr_handler"]
