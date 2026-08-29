"""pyannote.audio 3.1 speaker diarization over an in-memory waveform."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
import torch
from pyannote.audio import Pipeline  # type: ignore[import-untyped]


@dataclass(frozen=True, slots=True)
class DiarizedSegment:
    start_s: float
    end_s: float
    speaker: str
    """Raw pyannote cluster label, e.g. "SPEAKER_00" — pre-role-assignment."""


def load_pipeline(model_id: str, hf_token: str, *, device: str) -> Pipeline:
    pipeline = Pipeline.from_pretrained(model_id, use_auth_token=hf_token)
    if device == "cuda":
        pipeline.to(torch.device("cuda"))
    return pipeline


def diarize(
    pipeline: Pipeline, samples: npt.NDArray[np.float32], sample_rate: int
) -> list[DiarizedSegment]:
    waveform = torch.from_numpy(samples).unsqueeze(0)
    annotation = pipeline({"waveform": waveform, "sample_rate": sample_rate})
    segments = [
        DiarizedSegment(start_s=segment.start, end_s=segment.end, speaker=label)
        for segment, _track, label in annotation.itertracks(yield_label=True)
    ]
    segments.sort(key=lambda s: s.start_s)
    return segments


__all__ = ["DiarizedSegment", "diarize", "load_pipeline"]
