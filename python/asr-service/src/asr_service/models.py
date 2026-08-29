"""Loads faster-whisper and pyannote once at process startup and warms them
up with a short dummy inference, so the first real job doesn't pay lazy-init
cost and the /readyz gate (coda_worker_sdk.metrics.serve_http) has something
real to report on.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import numpy as np
import torch
from faster_whisper import WhisperModel  # type: ignore[import-untyped]
from pyannote.audio import Pipeline  # type: ignore[import-untyped]

from asr_service.config import AsrConfig
from asr_service.diarize import load_pipeline

logger = logging.getLogger(__name__)

_WARMUP_SAMPLES_S = 1.0


@dataclass(slots=True)
class ModelBundle:
    cfg: AsrConfig
    whisper: WhisperModel
    diarizer: Pipeline
    ready: bool = field(default=False)


def load_models(cfg: AsrConfig) -> ModelBundle:
    """Blocking — callers must run this via asyncio.to_thread so it doesn't
    stall the event loop during the (potentially multi-minute, first-run)
    download + load.
    """
    started = time.monotonic()
    logger.info(
        "loading faster-whisper model",
        extra={
            "extra_fields": {
                "model_size": cfg.model_size,
                "device": cfg.device,
                "compute_type": cfg.compute_type,
            }
        },
    )
    whisper = WhisperModel(
        cfg.model_size,
        device=cfg.device,
        compute_type=cfg.compute_type,
        download_root=cfg.whisper_cache_dir,
    )

    logger.info(
        "loading pyannote diarization pipeline",
        extra={"extra_fields": {"model": cfg.diarization_model}},
    )
    diarizer = load_pipeline(cfg.diarization_model, cfg.hf_token, device=cfg.device)

    bundle = ModelBundle(cfg=cfg, whisper=whisper, diarizer=diarizer)
    _warmup(bundle)
    bundle.ready = True
    logger.info(
        "models loaded and warmed up",
        extra={"extra_fields": {"wall_ms": int((time.monotonic() - started) * 1000)}},
    )
    return bundle


def _warmup(bundle: ModelBundle) -> None:
    silence = np.zeros(int(_WARMUP_SAMPLES_S * 16_000), dtype=np.float32)

    started = time.monotonic()
    segments, _info = bundle.whisper.transcribe(silence, language=bundle.cfg.language)
    list(segments)  # force generator execution
    logger.info(
        "faster-whisper warmup done",
        extra={"extra_fields": {"wall_ms": int((time.monotonic() - started) * 1000)}},
    )

    started = time.monotonic()
    try:
        waveform = torch.from_numpy(silence).unsqueeze(0)
        bundle.diarizer({"waveform": waveform, "sample_rate": 16_000})
    except Exception:
        # Pure silence commonly produces no speech segments (or an internal
        # empty-annotation edge case) — that's fine; warmup only needs the
        # model graph to have run once, not a meaningful diarization result.
        logger.info("diarization warmup produced no segments (expected for silence)")
    logger.info(
        "diarization warmup done",
        extra={"extra_fields": {"wall_ms": int((time.monotonic() - started) * 1000)}},
    )


__all__ = ["ModelBundle", "load_models"]
