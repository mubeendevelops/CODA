"""Audio preprocessing: decode, mono-ise, resample to 16kHz, loudness
normalise, and reject anything that isn't a usable consultation recording.

faster-whisper and pyannote.audio both accept an in-memory float32 waveform,
so preprocessing runs once and the same array feeds both models — no
intermediate WAV file, no double decode.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
from pydub import AudioSegment  # type: ignore[import-untyped]
from pydub.exceptions import CouldntDecodeError  # type: ignore[import-untyped]

from coda_worker_sdk.errors import FatalError

SUPPORTED_EXTENSIONS = frozenset({"wav", "mp3"})
TARGET_SAMPLE_RATE = 16_000


@dataclass(frozen=True, slots=True)
class AudioData:
    samples: npt.NDArray[np.float32]
    """float32, mono, TARGET_SAMPLE_RATE Hz, range approximately [-1, 1]."""
    sample_rate: int
    duration_s: float


def preprocess(
    raw: bytes,
    *,
    ext: str,
    target_loudness_dbfs: float,
    min_duration_s: float,
    max_duration_s: float,
) -> AudioData:
    ext = ext.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise FatalError(
            f"unsupported audio format {ext!r} (supported: {sorted(SUPPORTED_EXTENSIONS)})",
            code="AUDIO_UNSUPPORTED_FORMAT",
        )

    try:
        segment = AudioSegment.from_file(io.BytesIO(raw), format=ext)
    except CouldntDecodeError as exc:
        raise FatalError(
            f"audio file could not be decoded — corrupt or not a valid {ext} file: {exc}",
            code="AUDIO_DECODE_FAILED",
        ) from exc
    except Exception as exc:  # pydub/ffmpeg can raise a range of things on bad input
        raise FatalError(
            f"audio file could not be decoded: {exc}", code="AUDIO_DECODE_FAILED"
        ) from exc

    if len(segment) == 0:
        raise FatalError("audio file decoded to zero samples", code="AUDIO_EMPTY")

    segment = segment.set_channels(1).set_frame_rate(TARGET_SAMPLE_RATE).set_sample_width(2)

    duration_s = len(segment) / 1000.0
    if duration_s < min_duration_s:
        raise FatalError(
            f"audio duration {duration_s:.2f}s is below the minimum {min_duration_s}s",
            code="AUDIO_TOO_SHORT",
        )
    if duration_s > max_duration_s:
        raise FatalError(
            f"audio duration {duration_s:.2f}s exceeds the maximum {max_duration_s}s",
            code="AUDIO_TOO_LONG",
        )

    if segment.dBFS == float("-inf"):
        raise FatalError("audio appears to be pure silence", code="AUDIO_SILENT")

    gain = target_loudness_dbfs - segment.dBFS
    # Bound the applied gain: a file that's already near-silent but not
    # literally -inf dBFS would otherwise get an enormous gain that mostly
    # amplifies noise.
    gain = max(-24.0, min(24.0, gain))
    if not math.isnan(gain):
        segment = segment.apply_gain(gain)

    raw_samples = np.array(segment.get_array_of_samples())
    samples = raw_samples.astype(np.float32) / 32768.0
    # Guard against clipping introduced by the gain above.
    np.clip(samples, -1.0, 1.0, out=samples)

    return AudioData(samples=samples, sample_rate=TARGET_SAMPLE_RATE, duration_s=duration_s)


__all__ = ["AudioData", "SUPPORTED_EXTENSIONS", "TARGET_SAMPLE_RATE", "preprocess"]
