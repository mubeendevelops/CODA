"""WER and CER over coda_eval.normalize's documented normalisation
pipeline, applied identically to both sides before jiwer ever sees the text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import jiwer

from coda_eval.normalize import normalize_text


@dataclass(frozen=True, slots=True)
class WerCerResult:
    wer: float
    cer: float
    substitutions: int
    deletions: int
    insertions: int
    hits: int
    reference_word_count: int
    reference_char_count: int


def compute_wer_cer(reference: str, hypothesis: str) -> WerCerResult:
    ref_norm = normalize_text(reference)
    hyp_norm = normalize_text(hypothesis)

    if not ref_norm:
        # An empty normalised reference makes WER mathematically undefined
        # (division by zero words); treat a non-empty hypothesis as 100%
        # insertions rather than raising, so one degenerate item can't crash
        # a whole eval run.
        hyp_len = len(hyp_norm.split())
        return WerCerResult(
            wer=1.0 if hyp_len else 0.0,
            cer=1.0 if hypothesis.strip() else 0.0,
            substitutions=0,
            deletions=0,
            insertions=hyp_len,
            hits=0,
            reference_word_count=0,
            reference_char_count=0,
        )

    word_output = jiwer.process_words(ref_norm, hyp_norm)
    # jiwer.cer's declared return type is unioned with dict, for a
    # multi-reference call shape this never uses (single ref, single hyp
    # strings only always return a bare float in practice).
    cer_value = cast(float, jiwer.cer(ref_norm, hyp_norm))

    return WerCerResult(
        wer=word_output.wer,
        cer=cer_value,
        substitutions=word_output.substitutions,
        deletions=word_output.deletions,
        insertions=word_output.insertions,
        hits=word_output.hits,
        reference_word_count=len(ref_norm.split()),
        reference_char_count=len(ref_norm.replace(" ", "")),
    )


def aggregate_wer_cer(per_item: list[WerCerResult]) -> WerCerResult:
    """Corpus-level WER/CER: total edits over total reference units, not a
    mean of per-item ratios — averaging ratios over-weights short items.
    WER is weighted by reference word count, CER by reference char count
    (each metric's own natural unit).
    """
    total_sub = sum(r.substitutions for r in per_item)
    total_del = sum(r.deletions for r in per_item)
    total_ins = sum(r.insertions for r in per_item)
    total_hits = sum(r.hits for r in per_item)
    total_ref_words = sum(r.reference_word_count for r in per_item)
    total_ref_chars = sum(r.reference_char_count for r in per_item)

    wer = (total_sub + total_del + total_ins) / total_ref_words if total_ref_words else 0.0
    # jiwer.cer doesn't expose a separate character-edit-count breakdown the
    # way process_words does for WER, so the corpus CER is reconstructed as
    # a char-count-weighted average of the per-item ratios — exact if every
    # item's CER were computed from the same edit-distance definition
    # (it is; jiwer uses one implementation for both calls).
    cer = (
        sum(r.cer * r.reference_char_count for r in per_item) / total_ref_chars
        if total_ref_chars
        else 0.0
    )

    return WerCerResult(
        wer=wer,
        cer=cer,
        substitutions=total_sub,
        deletions=total_del,
        insertions=total_ins,
        hits=total_hits,
        reference_word_count=total_ref_words,
        reference_char_count=total_ref_chars,
    )


__all__ = ["WerCerResult", "aggregate_wer_cer", "compute_wer_cer"]
