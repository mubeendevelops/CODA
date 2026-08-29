"""faster-whisper transcription with word-level timestamps, chunked for long
audio with overlap so no single call has to hold an unbounded window, and
global timestamps reconstructed by trimming each chunk's words to its own
non-overlapping slice before concatenating.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
from faster_whisper import WhisperModel  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WordResult:
    text: str
    start_s: float
    end_s: float
    probability: float


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    words: list[WordResult]
    language: str
    duration_s: float


def _build_chunks(
    duration_s: float, chunk_length_s: float, overlap_s: float
) -> list[tuple[float, float]]:
    if duration_s <= chunk_length_s * 1.2:
        return [(0.0, duration_s)]
    chunks: list[tuple[float, float]] = []
    start = 0.0
    while start < duration_s:
        end = min(start + chunk_length_s, duration_s)
        chunks.append((start, end))
        if end >= duration_s:
            break
        start = end - overlap_s
    return chunks


def _merge_chunk_words(
    chunk_words: list[list[WordResult]],
    chunk_bounds: list[tuple[float, float]],
    overlap_s: float,
) -> list[WordResult]:
    """Keeps each chunk's words only within its own non-overlapping slice —
    the overlap region between chunk i and i+1 is split at its midpoint in
    global time, so a word spoken in the overlap is attributed to exactly
    one chunk's transcription of it, never duplicated or dropped.
    """
    n = len(chunk_bounds)
    merged: list[WordResult] = []
    for i, words in enumerate(chunk_words):
        lower = -math.inf if i == 0 else chunk_bounds[i][0] + overlap_s / 2
        upper = math.inf if i == n - 1 else chunk_bounds[i][1] - overlap_s / 2
        merged.extend(w for w in words if lower <= w.start_s < upper)
    merged.sort(key=lambda w: w.start_s)
    return merged


def transcribe(
    model: WhisperModel,
    samples: npt.NDArray[np.float32],
    *,
    sample_rate: int,
    language: str,
    chunk_length_s: float,
    chunk_overlap_s: float,
    on_chunk_done: Callable[[int, int], None] | None = None,
) -> TranscriptionResult:
    duration_s = len(samples) / sample_rate
    chunk_bounds = _build_chunks(duration_s, chunk_length_s, chunk_overlap_s)

    chunk_words: list[list[WordResult]] = []
    for i, (start_s, end_s) in enumerate(chunk_bounds):
        start_sample = int(start_s * sample_rate)
        end_sample = int(end_s * sample_rate)
        chunk_samples = samples[start_sample:end_sample]

        segments, _info = model.transcribe(
            chunk_samples,
            language=language,
            word_timestamps=True,
            vad_filter=True,
            # Chunks are transcribed independently — carrying "previous text"
            # context across a chunk boundary risks bleeding hallucinated
            # continuation across the overlap, exactly where correctness
            # matters most for the merge in _merge_chunk_words.
            condition_on_previous_text=False,
        )

        words: list[WordResult] = []
        for seg in segments:
            if not seg.words:
                continue
            for w in seg.words:
                words.append(
                    WordResult(
                        text=w.word,
                        start_s=w.start + start_s,
                        end_s=w.end + start_s,
                        probability=w.probability,
                    )
                )
        chunk_words.append(words)
        if on_chunk_done is not None:
            on_chunk_done(i + 1, len(chunk_bounds))

    merged = _merge_chunk_words(chunk_words, chunk_bounds, chunk_overlap_s)
    return TranscriptionResult(words=merged, language=language, duration_s=duration_s)


__all__ = ["TranscriptionResult", "WordResult", "transcribe"]
