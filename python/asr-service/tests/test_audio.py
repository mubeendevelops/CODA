import io
import struct
import wave

import pytest

from asr_service.audio import TARGET_SAMPLE_RATE, preprocess
from coda_worker_sdk.errors import FatalError


def _make_wav(
    *, seconds: float, freq_hz: float, sample_rate: int = 44100, amplitude: int = 8000
) -> bytes:
    n_samples = int(seconds * sample_rate)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(2)  # stereo input, to exercise mono-ising
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        import math

        frames = bytearray()
        for i in range(n_samples):
            sample = int(amplitude * math.sin(2 * math.pi * freq_hz * i / sample_rate))
            frames += struct.pack("<hh", sample, sample)
        wf.writeframes(bytes(frames))
    return buf.getvalue()


def _make_silence_wav(*, seconds: float, sample_rate: int = 16000) -> bytes:
    n_samples = int(seconds * sample_rate)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * n_samples)
    return buf.getvalue()


def test_preprocess_resamples_and_monoises() -> None:
    raw = _make_wav(seconds=1.0, freq_hz=440.0)
    audio = preprocess(
        raw, ext="wav", target_loudness_dbfs=-20.0, min_duration_s=0.1, max_duration_s=60.0
    )
    assert audio.sample_rate == TARGET_SAMPLE_RATE
    assert audio.samples.ndim == 1
    assert abs(audio.duration_s - 1.0) < 0.05
    assert audio.samples.dtype.name == "float32"
    assert audio.samples.max() <= 1.0 and audio.samples.min() >= -1.0


def test_preprocess_rejects_unsupported_extension() -> None:
    with pytest.raises(FatalError) as exc:
        preprocess(
            b"whatever", ext="ogg", target_loudness_dbfs=-20.0, min_duration_s=0.1,
            max_duration_s=60.0,
        )
    assert exc.value.code == "AUDIO_UNSUPPORTED_FORMAT"


def test_preprocess_rejects_corrupt_file() -> None:
    with pytest.raises(FatalError) as exc:
        preprocess(
            b"not actually audio data",
            ext="wav",
            target_loudness_dbfs=-20.0,
            min_duration_s=0.1,
            max_duration_s=60.0,
        )
    assert exc.value.code == "AUDIO_DECODE_FAILED"


def test_preprocess_rejects_silence() -> None:
    raw = _make_silence_wav(seconds=1.0)
    with pytest.raises(FatalError) as exc:
        preprocess(
            raw, ext="wav", target_loudness_dbfs=-20.0, min_duration_s=0.1, max_duration_s=60.0
        )
    assert exc.value.code == "AUDIO_SILENT"


def test_preprocess_rejects_too_short() -> None:
    raw = _make_wav(seconds=0.1, freq_hz=440.0)
    with pytest.raises(FatalError) as exc:
        preprocess(
            raw, ext="wav", target_loudness_dbfs=-20.0, min_duration_s=1.0, max_duration_s=60.0
        )
    assert exc.value.code == "AUDIO_TOO_SHORT"
