"""Aggregate field-level precision/recall/F1 across a gold/hypothesis
consultation set (plan.md Phase 5), built on top of the per-value matching
rules in `metrics/field_match.py`.

Scoring convention, stated explicitly since it is a modelling choice:

- **Scalar fields** (`chief_complaint`, `hopi`, `examination_findings`,
  `treatment_plan`): gold and hypothesis are each either a single value or
  null.
    - both null -> a true negative (correct absence; not counted in P/R/F1,
      any more than a NER scorer counts "no entity here" as a hit)
    - gold null, hyp non-null -> one false positive (a **hallucinated
      field** — the field-level structural counterpart to the LLM-judge
      hallucination score in `metrics/hallucination.py`, which checks
      *content* faithfulness rather than presence)
    - gold non-null, hyp null -> one false negative (an **empty field** —
      the system omitted something it should have captured)
    - both non-null, `field_match.match_values` agrees -> one true positive
    - both non-null, values disagree -> **one false positive and one false
      negative** (standard QA/NER convention for "wrong answer given": the
      system's claim is wrong, precision-wise, and the gold answer was
      still not recovered, recall-wise — not scored as a no-op)
- **List fields** (`past_medical_history`, `medications`, `allergies`,
  `provisional_diagnosis`, `investigations_advised`): gold and hypothesis
  are each a list of values, greedily bipartite-matched (each gold item
  claims at most one matching hypothesis item, first-fit in gold order —
  fields are short enough in practice that greedy vs. optimal assignment
  never changes the count) via the same per-field matching rule. Matched
  pairs are true positives; unmatched gold items are false negatives;
  unmatched hypothesis items are false positives. Empty/hallucinated-field
  rates for list fields are defined at the list level: gold list empty but
  hyp list non-empty (hallucinated), or gold list non-empty but hyp list
  empty (empty).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

from coda_eval.metrics.field_match import match_values

SCALAR_FIELDS = ("chief_complaint", "hopi", "examination_findings", "treatment_plan")
LIST_FIELDS = (
    "past_medical_history",
    "medications",
    "allergies",
    "provisional_diagnosis",
    "investigations_advised",
)
ALL_FIELDS = SCALAR_FIELDS + LIST_FIELDS


@dataclass(slots=True)
class FieldCounts:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0
    """Both gold and hypothesis null/empty — a correct absence. Not used in
    precision/recall/F1 (there is nothing to recall), but kept so an
    "always says nothing" hypothesis is visibly different from a genuinely
    accurate one when the counts are inspected directly."""
    empty_opportunities: int = 0
    """Items where gold is non-null/non-empty — the denominator for the
    empty-field rate."""
    empty_count: int = 0
    """Of those, how many the hypothesis left null/empty."""
    hallucination_opportunities: int = 0
    """Items where gold is null/empty — the denominator for the
    structural hallucinated-field rate."""
    hallucination_count: int = 0
    """Of those, how many the hypothesis filled in anyway."""

    def precision(self) -> float | None:
        denom = self.tp + self.fp
        return self.tp / denom if denom else None

    def recall(self) -> float | None:
        denom = self.tp + self.fn
        return self.tp / denom if denom else None

    def f1(self) -> float | None:
        p, r = self.precision(), self.recall()
        if p is None or r is None or (p + r) == 0:
            return None
        return 2 * p * r / (p + r)

    def empty_rate(self) -> float | None:
        return self.empty_count / self.empty_opportunities if self.empty_opportunities else None

    def hallucination_rate(self) -> float | None:
        return (
            self.hallucination_count / self.hallucination_opportunities
            if self.hallucination_opportunities
            else None
        )

    def __iadd__(self, other: FieldCounts) -> FieldCounts:
        self.tp += other.tp
        self.fp += other.fp
        self.fn += other.fn
        self.tn += other.tn
        self.empty_opportunities += other.empty_opportunities
        self.empty_count += other.empty_count
        self.hallucination_opportunities += other.hallucination_opportunities
        self.hallucination_count += other.hallucination_count
        return self


@dataclass(slots=True)
class FieldScoringResult:
    per_field: dict[str, FieldCounts] = field(default_factory=dict)
    overall: FieldCounts = field(default_factory=FieldCounts)


def _scalar_value(entry: dict[str, object] | None) -> str | None:
    if entry is None:
        return None
    value = entry.get("value")
    return value if isinstance(value, str) and value != "" else None


def score_scalar_field(
    gold: dict[str, object] | None, hyp: dict[str, object] | None, *, field_name: str
) -> FieldCounts:
    counts = FieldCounts()
    gold_value = _scalar_value(gold)
    hyp_value = _scalar_value(hyp)

    if gold_value is None:
        counts.hallucination_opportunities = 1
        if hyp_value is not None:
            counts.fp = 1
            counts.hallucination_count = 1
        else:
            counts.tn = 1
        return counts

    counts.empty_opportunities = 1
    if hyp_value is None:
        counts.fn = 1
        counts.empty_count = 1
        return counts

    if match_values(gold_value, hyp_value, field_name).matched:
        counts.tp = 1
    else:
        counts.fp = 1
        counts.fn = 1
    return counts


def _list_values(entries: list[dict[str, object]] | None) -> list[str]:
    if not entries:
        return []
    out = []
    for e in entries:
        v = e.get("value")
        if isinstance(v, str) and v != "":
            out.append(v)
    return out


def score_list_field(
    gold: list[dict[str, object]] | None, hyp: list[dict[str, object]] | None, *, field_name: str
) -> FieldCounts:
    counts = FieldCounts()
    gold_values = _list_values(gold)
    hyp_values = _list_values(hyp)

    if not gold_values:
        counts.hallucination_opportunities = 1
        if hyp_values:
            counts.hallucination_count = 1
        else:
            counts.tn = 1
    else:
        counts.empty_opportunities = 1
        if not hyp_values:
            counts.empty_count = 1

    unmatched_hyp = list(range(len(hyp_values)))
    for g in gold_values:
        match_idx = None
        for i in unmatched_hyp:
            if match_values(g, hyp_values[i], field_name).matched:
                match_idx = i
                break
        if match_idx is not None:
            counts.tp += 1
            unmatched_hyp.remove(match_idx)
        else:
            counts.fn += 1
    counts.fp += len(unmatched_hyp)
    return counts


def score_consultation(
    gold_fields: dict[str, object], hyp_fields: dict[str, object]
) -> dict[str, FieldCounts]:
    """Scores one consultation's fields against its gold record. Both
    `gold_fields` and `hyp_fields` are the `fields` dict of a gold record
    (gold_schema.py) / an extraction result (nlp_service/schema.py) —
    identical field names and value shape by construction (module docstring
    of gold_schema.py)."""
    result: dict[str, FieldCounts] = {}
    for f in SCALAR_FIELDS:
        gold_scalar = cast("dict[str, object] | None", gold_fields.get(f))
        hyp_scalar = cast("dict[str, object] | None", hyp_fields.get(f))
        result[f] = score_scalar_field(gold_scalar, hyp_scalar, field_name=f)
    for f in LIST_FIELDS:
        gold_list = cast("list[dict[str, object]] | None", gold_fields.get(f))
        hyp_list = cast("list[dict[str, object]] | None", hyp_fields.get(f))
        result[f] = score_list_field(gold_list, hyp_list, field_name=f)
    return result


def aggregate(per_consultation: list[dict[str, FieldCounts]]) -> FieldScoringResult:
    """Sums per-field counts across every consultation — a **micro**
    aggregate (total edits over total opportunities), matching
    claude_context.md decision #63's rule for WER/CER/DER: a mean of
    per-consultation ratios would over-weight consultations with few
    opportunities."""
    result = FieldScoringResult()
    for f in ALL_FIELDS:
        result.per_field[f] = FieldCounts()
    for consultation in per_consultation:
        for f, counts in consultation.items():
            result.per_field[f] += counts
            result.overall += counts
    return result


__all__ = [
    "ALL_FIELDS",
    "LIST_FIELDS",
    "SCALAR_FIELDS",
    "FieldCounts",
    "FieldScoringResult",
    "aggregate",
    "score_consultation",
    "score_list_field",
    "score_scalar_field",
]
