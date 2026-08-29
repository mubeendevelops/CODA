"""asr-service environment configuration.

Model choice (faster-whisper size/compute/device, cache location) is a
service-level deployment knob, not a per-job value yet — the envelope
(proto/coda/v1/envelope.proto) carries no per-job model selection, and
RunConfig.asr_backend/asr_model (proto/coda/v1/runconfig.proto) is not wired
through to workers until the ablation harness (plan.md Phase 6+) needs it.
`language`, meanwhile, IS read per-job when present (envelope.labels["language"]),
falling back to this config's default — see worker.py.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _require(name: str) -> str:
    val = os.environ.get(name, "")
    if not val:
        raise RuntimeError(f"asr_service: required environment variable {name} is not set")
    return val


def _get(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True, slots=True)
class AsrConfig:
    model_size: str
    """faster-whisper model size or path, e.g. "medium" (decided: medium/int8/CPU)."""
    device: str
    """"cpu" or "cuda"."""
    compute_type: str
    """CTranslate2 compute type, e.g. "int8", "float16", "float32"."""
    language: str
    """Default transcription language. Never hardcoded elsewhere in the ASR
    code path — this is the one place "en" is allowed to appear as a
    default value (claude_context.md §2.1)."""
    model_cache_dir: str
    """Root directory both faster-whisper and pyannote/huggingface_hub cache
    weights under (a Docker named volume — see docker-compose.yml)."""
    hf_token: str
    """Required for the gated pyannote/speaker-diarization-3.1 checkpoint."""
    diarization_model: str
    groq_api_key: str
    """Required for the few-shot Doctor/Patient role classifier."""
    role_model: str
    """Groq model id used for role classification, e.g. llama-3.1-8b-instant."""
    role_confidence_threshold: float
    """Below this, a role assignment is still recorded but flagged uncertain
    (logged + surfaced via SpeakerCluster.confidence) rather than trusted."""
    chunk_length_s: float
    """Long-audio chunk size for the ASR pass. Audio shorter than this
    (times a small slack factor) is transcribed in one call."""
    chunk_overlap_s: float
    target_loudness_dbfs: float
    """Target level for RMS-based loudness normalisation during preprocessing."""
    max_audio_duration_s: float
    min_audio_duration_s: float
    pause_turn_break_s: float
    """A same-speaker gap longer than this still starts a new turn."""

    @classmethod
    def from_env(cls) -> AsrConfig:
        hf_token = _require("HF_TOKEN")
        groq_api_key = _require("GROQ_API_KEY")
        return cls(
            model_size=_get("ASR_MODEL_SIZE", "medium"),
            device=_get("ASR_DEVICE", "cpu"),
            compute_type=_get("ASR_COMPUTE_TYPE", "int8"),
            language=_get("ASR_LANGUAGE", "en"),
            model_cache_dir=_get("MODEL_CACHE_DIR", "/model-cache"),
            hf_token=hf_token,
            diarization_model=_get("ASR_DIARIZATION_MODEL", "pyannote/speaker-diarization-3.1"),
            groq_api_key=groq_api_key,
            role_model=_get("ASR_ROLE_MODEL", "llama-3.1-8b-instant"),
            role_confidence_threshold=float(_get("ASR_ROLE_CONFIDENCE_THRESHOLD", "0.6")),
            chunk_length_s=float(_get("ASR_CHUNK_LENGTH_S", "300")),
            chunk_overlap_s=float(_get("ASR_CHUNK_OVERLAP_S", "5")),
            target_loudness_dbfs=float(_get("ASR_TARGET_LOUDNESS_DBFS", "-20")),
            max_audio_duration_s=float(_get("ASR_MAX_AUDIO_DURATION_S", str(4 * 3600))),
            min_audio_duration_s=float(_get("ASR_MIN_AUDIO_DURATION_S", "0.5")),
            pause_turn_break_s=float(_get("ASR_PAUSE_TURN_BREAK_S", "1.5")),
        )

    @property
    def whisper_cache_dir(self) -> str:
        return f"{self.model_cache_dir}/faster-whisper"


__all__ = ["AsrConfig"]
