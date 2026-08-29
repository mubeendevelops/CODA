"""DER (Diarization Error Rate) via pyannote.metrics, with its standard
component breakdown: missed detection, false alarm, and speaker confusion.
"""

from __future__ import annotations

from dataclasses import dataclass

from pyannote.core import Annotation  # type: ignore[import-untyped]
from pyannote.metrics.diarization import DiarizationErrorRate  # type: ignore[import-untyped]


@dataclass(frozen=True, slots=True)
class DerResult:
    der: float
    missed_detection: float
    false_alarm: float
    confusion: float
    total_reference_speech_s: float


def _result_from_totals(
    missed: float, false_alarm: float, confusion: float, total: float
) -> DerResult:
    der = (missed + false_alarm + confusion) / total if total else 0.0
    return DerResult(
        der=der,
        missed_detection=missed / total if total else 0.0,
        false_alarm=false_alarm / total if total else 0.0,
        confusion=confusion / total if total else 0.0,
        total_reference_speech_s=total,
    )


def compute_der(reference: Annotation, hypothesis: Annotation) -> DerResult:
    """Single-item DER. `DiarizationErrorRate` is stateful (it accumulates
    across calls for corpus-level reporting), so this always uses a fresh
    instance — use `DerAccumulator` when a corpus-level total is wanted.
    """
    metric = DiarizationErrorRate()
    detail = metric(reference, hypothesis, detailed=True)
    return _result_from_totals(
        detail["missed detection"], detail["false alarm"], detail["confusion"], detail["total"]
    )


class DerAccumulator:
    """Corpus-level DER: totals total-error over total-reference-speech
    across every `add` call, not a mean of per-item ratios — same principle
    `wer_cer.aggregate_wer_cer` applies for WER/CER. Sums the raw component
    totals `DiarizationErrorRate.__call__` returns per item, rather than
    relying on the library's own cross-call accumulation or its `.report()`
    formatting (both are implementation details this doesn't need to lean on).
    """

    def __init__(self) -> None:
        self._missed = 0.0
        self._false_alarm = 0.0
        self._confusion = 0.0
        self._total = 0.0

    def add(self, reference: Annotation, hypothesis: Annotation) -> DerResult:
        metric = DiarizationErrorRate()
        detail = metric(reference, hypothesis, detailed=True)
        self._missed += detail["missed detection"]
        self._false_alarm += detail["false alarm"]
        self._confusion += detail["confusion"]
        self._total += detail["total"]
        return _result_from_totals(
            detail["missed detection"], detail["false alarm"], detail["confusion"], detail["total"]
        )

    def result(self) -> DerResult:
        return _result_from_totals(self._missed, self._false_alarm, self._confusion, self._total)


__all__ = ["DerAccumulator", "DerResult", "compute_der"]
