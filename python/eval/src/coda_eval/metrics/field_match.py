"""Per-field value-matching rules for field-level precision/recall/F1
(plan.md Phase 5). Three rule kinds, chosen per field and documented in
docs/eval/annotation_guide.md's "Field matching rules" section:

- **exact** — case/whitespace-normalized string equality, no fuzz. Reserved
  for `allergies`: a fuzzy false-match between two short, safety-critical
  strings ("penicillin" vs "amoxicillin") is a dangerous failure mode a
  clinical eval must not paper over with a lenient rule.
- **normalized** — case/punctuation-normalized + `rapidfuzz` ratio above a
  threshold. For `past_medical_history`, `medications`, and
  `investigations_advised`: short, fairly canonical clinical noun phrases
  where phrasing/formatting varies (dose notation, word order) but the
  underlying vocabulary doesn't need a semantic model to reconcile.
- **semantic** — cosine similarity of local MiniLM sentence embeddings
  (`sentence-transformers/all-MiniLM-L6-v2`, already the project's chosen
  local/free embedding model — claude_context.md §4) above a threshold. For
  the four free-text scalar fields (`chief_complaint`, `hopi`,
  `examination_findings`, `treatment_plan`) and `provisional_diagnosis`,
  where clinical synonymy ("URTI" vs "upper respiratory tract infection") or
  narrative paraphrase is common enough that string-level rules would
  systematically undercount correct extractions.

Zero API cost either way — both `rapidfuzz` and the embedding model run
locally, consistent with claude_context.md §8's token-budget discipline (a
metric that costs Groq quota on every eval run would compete with the
system-under-test for the same daily cap).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache

from rapidfuzz import fuzz

EXACT = "exact"
NORMALIZED = "normalized"
SEMANTIC = "semantic"

# One rule per field, chosen per the module docstring's rationale.
FIELD_MATCH_RULES: dict[str, str] = {
    "chief_complaint": SEMANTIC,
    "hopi": SEMANTIC,
    "past_medical_history": NORMALIZED,
    "medications": NORMALIZED,
    "allergies": EXACT,
    "examination_findings": SEMANTIC,
    "provisional_diagnosis": SEMANTIC,
    "investigations_advised": NORMALIZED,
    "treatment_plan": SEMANTIC,
}

# Per-rule default thresholds. Semantic thresholds are lower for longer,
# more diffuse narrative fields (hopi, examination_findings, treatment_plan)
# than for short, more literal ones (chief_complaint, provisional_diagnosis)
# — a longer gold/hyp pair naturally has lower cosine similarity even when
# both are fully correct, simply from paraphrase surface area.
SEMANTIC_THRESHOLDS: dict[str, float] = {
    "chief_complaint": 0.60,
    "hopi": 0.55,
    "examination_findings": 0.55,
    "provisional_diagnosis": 0.65,
    "treatment_plan": 0.55,
}
NORMALIZED_THRESHOLD = 85.0  # rapidfuzz ratio is 0-100
_INVESTIGATIONS_ALIAS_THRESHOLD = 90.0  # short abbreviations need a higher bar

_PUNCT_RE = re.compile(r"[^\w\s]")
_WHITESPACE_RE = re.compile(r"\s+")

# Common test-name aliases so "CBC" and "complete blood count" normalize to
# the same canonical token before the fuzzy fallback runs — investigations
# are exactly the case where two *different* short abbreviations (CBC vs
# CMP) are edit-distance-close enough that fuzz alone risks a false match,
# so alias canonicalization runs first and fuzz is the fallback, not the
# primary signal, for this one field.
_INVESTIGATION_ALIASES: dict[str, str] = {
    "complete blood count": "cbc",
    "full blood count": "cbc",
    "fbc": "cbc",
    "chest x ray": "cxr",
    "chest xray": "cxr",
    "electrocardiogram": "ecg",
    "ekg": "ecg",
    "midstream urine": "msu",
    "mid stream urine": "msu",
    "urine dipstick": "urine dip",
}


def _normalize_text(text: str) -> str:
    text = text.lower().strip()
    text = _PUNCT_RE.sub(" ", text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def _canonicalize_investigation(text: str) -> str:
    norm = _normalize_text(text)
    return _INVESTIGATION_ALIASES.get(norm, norm)


@cache
def _embedding_model() -> object:
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer("all-MiniLM-L6-v2")


def _cosine_similarity(a: str, b: str) -> float:
    import numpy as np

    model = _embedding_model()
    vecs = model.encode([a, b], normalize_embeddings=True)  # type: ignore[attr-defined]
    return float(np.dot(vecs[0], vecs[1]))


def exact_match(gold: str, hyp: str) -> bool:
    return _normalize_text(gold) == _normalize_text(hyp)


def normalized_match(
    gold: str, hyp: str, *, field: str | None = None, threshold: float | None = None
) -> bool:
    if field == "investigations_advised":
        if _canonicalize_investigation(gold) == _canonicalize_investigation(hyp):
            return True
        threshold = threshold or _INVESTIGATIONS_ALIAS_THRESHOLD
        return fuzz.ratio(_normalize_text(gold), _normalize_text(hyp)) >= threshold

    threshold = threshold if threshold is not None else NORMALIZED_THRESHOLD
    return fuzz.ratio(_normalize_text(gold), _normalize_text(hyp)) >= threshold


def semantic_match(gold: str, hyp: str, *, field: str, threshold: float | None = None) -> bool:
    threshold = threshold if threshold is not None else SEMANTIC_THRESHOLDS.get(field, 0.6)
    return _cosine_similarity(gold, hyp) >= threshold


@dataclass(frozen=True, slots=True)
class MatchExplanation:
    matched: bool
    rule: str
    score: float | None
    """rapidfuzz ratio (0-100) for normalized, cosine similarity (0-1) for
    semantic, None for exact (binary by construction)."""


def match_values(gold: str, hyp: str, field: str) -> MatchExplanation:
    """Applies whichever rule `FIELD_MATCH_RULES[field]` names, returning the
    verdict plus the raw score for audit/debugging (e.g. a case study
    showing *why* two phrasings were or weren't counted as a match).
    """
    rule = FIELD_MATCH_RULES[field]
    if rule == EXACT:
        return MatchExplanation(matched=exact_match(gold, hyp), rule=rule, score=None)
    if rule == NORMALIZED:
        is_investigations = field == "investigations_advised"
        threshold = _INVESTIGATIONS_ALIAS_THRESHOLD if is_investigations else NORMALIZED_THRESHOLD
        canon_gold = (
            _canonicalize_investigation(gold) if is_investigations else _normalize_text(gold)
        )
        canon_hyp = _canonicalize_investigation(hyp) if is_investigations else _normalize_text(hyp)
        if canon_gold == canon_hyp:
            return MatchExplanation(matched=True, rule=rule, score=100.0)
        score = fuzz.ratio(_normalize_text(gold), _normalize_text(hyp))
        return MatchExplanation(matched=score >= threshold, rule=rule, score=score)
    if rule == SEMANTIC:
        score = _cosine_similarity(gold, hyp)
        threshold = SEMANTIC_THRESHOLDS.get(field, 0.6)
        return MatchExplanation(matched=score >= threshold, rule=rule, score=score)
    raise ValueError(f"unknown match rule {rule!r} for field {field!r}")  # pragma: no cover


__all__ = [
    "EXACT",
    "FIELD_MATCH_RULES",
    "NORMALIZED",
    "NORMALIZED_THRESHOLD",
    "SEMANTIC",
    "SEMANTIC_THRESHOLDS",
    "MatchExplanation",
    "exact_match",
    "match_values",
    "normalized_match",
    "semantic_match",
]
