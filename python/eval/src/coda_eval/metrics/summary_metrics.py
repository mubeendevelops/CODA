"""Summarization metrics for the consultation `summary` field (plan.md
Phase 5): ROUGE-L and BERTScore against the gold summary.

BERTScore model: `distilbert-base-uncased`, not the library's own default
(`roberta-large`) — this project runs on a CPU laptop with no fixed
deadline but a real token/wall-clock budget elsewhere (claude_context.md
§8), and distilbert is roughly 6x smaller with BERTScore's own published
correlation-to-human-judgment numbers close enough to roberta-large for a
relative (baseline-vs-GoT) comparison, which is what this project actually
needs — it is not claiming an absolute state-of-the-art BERTScore number.
Recorded here rather than left to the library default so a future run
can't silently get a different, non-comparable number from a library
upgrade that changes its default model.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache

_BERTSCORE_MODEL = "distilbert-base-uncased"


@dataclass(frozen=True, slots=True)
class RougeLResult:
    precision: float
    recall: float
    fmeasure: float


@dataclass(frozen=True, slots=True)
class BertScoreResult:
    precision: float
    recall: float
    f1: float
    model: str


@cache
def _rouge_scorer() -> object:
    from rouge_score import rouge_scorer

    return rouge_scorer.RougeScorer(["rougeL"], use_stemmer=True)


def rouge_l(gold_summary: str, hyp_summary: str) -> RougeLResult:
    scorer = _rouge_scorer()
    score = scorer.score(gold_summary, hyp_summary)["rougeL"]  # type: ignore[attr-defined]
    return RougeLResult(precision=score.precision, recall=score.recall, fmeasure=score.fmeasure)


def bertscore_batch(gold_summaries: list[str], hyp_summaries: list[str]) -> list[BertScoreResult]:
    """Batched, not per-pair: loading `distilbert-base-uncased` per call
    would dominate wall-clock on a CPU laptop for anything beyond a
    handful of consultations."""
    import bert_score

    if len(gold_summaries) != len(hyp_summaries):
        raise ValueError("gold_summaries and hyp_summaries must be the same length")
    if not gold_summaries:
        return []

    precision, recall, f1 = bert_score.score(
        hyp_summaries, gold_summaries, lang="en", model_type=_BERTSCORE_MODEL, verbose=False
    )
    return [
        BertScoreResult(precision=float(p), recall=float(r), f1=float(f), model=_BERTSCORE_MODEL)
        for p, r, f in zip(precision.tolist(), recall.tolist(), f1.tolist(), strict=True)
    ]


def bertscore_one(gold_summary: str, hyp_summary: str) -> BertScoreResult:
    return bertscore_batch([gold_summary], [hyp_summary])[0]


__all__ = [
    "BertScoreResult",
    "RougeLResult",
    "bertscore_batch",
    "bertscore_one",
    "rouge_l",
]
