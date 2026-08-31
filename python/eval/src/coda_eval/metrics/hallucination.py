"""LLM-as-judge hallucination scoring (plan.md Phase 5): for every non-null
extracted field value and every summary sentence, ask a judge model whether
the transcript actually supports that specific claim, per the written
rubric at `python/eval/prompts/en/v1/hallucination_judge_*.md`.

Two different grounding scopes, both scored through the same code path:

- **Field claims** are judged against ONLY their own cited `source_turn_ids`
  — that is the whole point of citation-grounded extraction (claude_context.md
  §3: "an unsupported non-null value is a hallucination"). A right answer
  with the wrong citation is still scored UNSUPPORTED (the rubric says so
  explicitly), since downstream provenance/audit tooling trusts the
  citation, not just the value.
- **Summary claims** have no per-sentence citation in nlp_service's schema
  (summary.py's output is free text — decision predates this eval work).
  Each sentence is instead judged against the **union of every turn any
  field claim cited** in this consultation (falling back to every turn in
  the transcript only if there were no field claims at all to build that
  union from) — the practical analogue of "unsupported by any cited turn"
  when there is nothing narrower to cite for the summary specifically.
  This is a documented design choice, not a silent gap, and it was
  tightened from an earlier "whole transcript" design after that version
  proved unworkable in practice (see below).

**Why the judge only ever sees cited turns, never the full transcript.**
`openai/gpt-oss-20b`'s Groq free tier caps requests at 8000 tokens per
minute — a single real consultation transcript (PriMock57 items run
80-140 turns) already exceeds that on its own, before any claims or
rubric text are added, discovered empirically as a live `413 Request Too
Large` (`tokens per minute (TPM): Limit 8000, Requested ~8200-10500`)
against every one of the 8 gold-annotated items in the first Phase 5
baseline run. Sending only the turns actually cited by at least one claim
— field claims cite a handful of turns each, not the whole consultation —
keeps a real request within budget without weakening what the rubric
checks: citation-faithfulness is a claim against ITS OWN cited turns
anyway, so a turn no claim cites was never going to change the verdict.

All claims for one consultation are batched into ONE judge call (the same
field-batching principle as extraction/scoring elsewhere — claude_context.md
§8), not one call per claim, since a 10-minute consultation can carry
15-20+ claims and per-claim calls would multiply token/request cost for no
benefit the judge model needs a whole-note view anyway to check
cross-turn corrections.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from coda_eval import prompts as eval_prompts
from coda_eval.gold import ReferenceTurn, transcript_turns_text
from nlp_service.llm.client import LLMClient

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True, slots=True)
class Claim:
    claim_id: str
    field: str
    """Field name for a field claim, or 'summary' for a summary-sentence claim."""
    value: str
    source_turn_ids: list[int]


@dataclass(frozen=True, slots=True)
class Verdict:
    claim_id: str
    supported: bool
    rationale: str


def _scalar_claims(fields: dict[str, object], field_name: str) -> list[Claim]:
    entry = fields.get(field_name)
    if not isinstance(entry, dict):
        return []
    value = entry.get("value")
    if not isinstance(value, str) or value == "":
        return []
    ids = entry.get("source_turn_ids") or []
    assert isinstance(ids, list)
    return [
        Claim(
            claim_id=f"field:{field_name}",
            field=field_name,
            value=value,
            source_turn_ids=list(ids),
        )
    ]


def _list_claims(fields: dict[str, object], field_name: str) -> list[Claim]:
    entries = fields.get(field_name) or []
    assert isinstance(entries, list)
    claims = []
    for idx, entry in enumerate(entries):
        assert isinstance(entry, dict)
        value = entry.get("value")
        if not isinstance(value, str) or value == "":
            continue
        ids = entry.get("source_turn_ids") or []
        assert isinstance(ids, list)
        claims.append(
            Claim(
                claim_id=f"field:{field_name}:{idx}",
                field=field_name,
                value=value,
                source_turn_ids=list(ids),
            )
        )
    return claims


_SCALAR_FIELDS = ("chief_complaint", "hopi", "examination_findings", "treatment_plan")
_LIST_FIELDS = (
    "past_medical_history",
    "medications",
    "allergies",
    "provisional_diagnosis",
    "investigations_advised",
)


def claims_from_note(
    fields: dict[str, object], summary: str, *, all_turn_ids: list[int]
) -> list[Claim]:
    """Extracts every judgeable claim from one consultation's extraction
    output. `fields` is the `fields` dict shape shared by gold_schema.py and
    nlp_service/schema.py (value/source_turn_ids per entry).

    `all_turn_ids` is only a fallback: summary-sentence claims are cited
    against the union of every turn a field claim cited, since that is
    both a much smaller request (module docstring) and arguably the more
    honest scope — the summary is supposed to restate what the fields
    already grounded, not introduce new transcript coverage of its own. It
    falls back to `all_turn_ids` only when there are no field claims to
    build that union from (e.g. every field came back null)."""
    claims: list[Claim] = []
    for f in _SCALAR_FIELDS:
        claims.extend(_scalar_claims(fields, f))
    for f in _LIST_FIELDS:
        claims.extend(_list_claims(fields, f))

    cited_by_fields = sorted({tid for c in claims for tid in c.source_turn_ids})
    summary_turn_ids = cited_by_fields or list(all_turn_ids)

    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(summary.strip()) if s.strip()]
    for idx, sentence in enumerate(sentences):
        claims.append(
            Claim(
                claim_id=f"summary:{idx}",
                field="summary",
                value=sentence,
                source_turn_ids=list(summary_turn_ids),
            )
        )
    return claims


def _claims_to_judge_json(claims: list[Claim]) -> str:
    payload = [
        {
            "claim_id": c.claim_id,
            "field": c.field,
            "value": c.value,
            "cited_turn_ids": c.source_turn_ids,
        }
        for c in claims
    ]
    return json.dumps(payload, ensure_ascii=False)


class HallucinationJudgeError(RuntimeError):
    """Raised when the judge model's response can't be parsed into one
    verdict per claim — a malformed judge response, not a claim about the
    transcript itself, so callers should treat this as a run-level failure
    (log and skip the consultation) rather than score it as anything."""


_MIN_JUDGE_MAX_TOKENS = 4000
_JUDGE_MAX_TOKENS_PER_CLAIM = 300
"""A reasoning model (`openai/gpt-oss-20b`) can spend most of its output
budget on hidden reasoning before ever emitting the final `content` JSON —
discovered empirically as a Groq `json_validate_failed` with an empty
`failed_generation` once the claim count (and therefore expected output
size) grew past what fit in the provider's default max_tokens. Scaling with
claim count, with a floor, is cheaper than guessing one fixed constant that
works for both a 2-claim and a 20-claim consultation."""


async def judge_claims(
    *,
    llm_client: LLMClient,
    model: str,
    turns: list[ReferenceTurn],
    claims: list[Claim],
    timeout_s: float = 90.0,
    language: str = eval_prompts.DEFAULT_LANGUAGE,
) -> list[Verdict]:
    """`turns` is the full reference transcript — only the subset any claim
    actually cites is ever rendered into the prompt (module docstring: the
    judge model's free-tier TPM cap can't fit a full 80-140-turn PriMock57
    transcript in one request)."""
    if not claims:
        return []

    cited_ids = {tid for c in claims for tid in c.source_turn_ids}
    cited_turns = [t for t in turns if t.turn_id in cited_ids]
    filtered_transcript_text = transcript_turns_text(cited_turns)

    pr = eval_prompts.load_hallucination_judge_prompts(language=language)
    user_prompt = pr.user_template.format(
        transcript_turns=filtered_transcript_text,
        claims_json=_claims_to_judge_json(claims),
    )
    max_tokens = max(_MIN_JUDGE_MAX_TOKENS, _JUDGE_MAX_TOKENS_PER_CLAIM * len(claims))

    completion = await llm_client.complete(
        model=model,
        system_prompt=pr.system,
        user_prompt=user_prompt,
        temperature=0.0,
        timeout_s=timeout_s,
        max_tokens=max_tokens,
        json_mode=True,
    )

    try:
        data = json.loads(completion.content)
        raw_verdicts = data["verdicts"]
        if not isinstance(raw_verdicts, list):
            raise TypeError("verdicts is not a list")
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise HallucinationJudgeError(
            f"judge response was not the expected {{'verdicts': [...]}} shape: {exc}"
        ) from exc

    by_id = {}
    for v in raw_verdicts:
        if not isinstance(v, dict) or "claim_id" not in v or "supported" not in v:
            raise HallucinationJudgeError(f"malformed verdict entry: {v!r}")
        by_id[v["claim_id"]] = Verdict(
            claim_id=str(v["claim_id"]),
            supported=bool(v["supported"]),
            rationale=str(v.get("rationale", "")),
        )

    missing = [c.claim_id for c in claims if c.claim_id not in by_id]
    if missing:
        raise HallucinationJudgeError(f"judge omitted verdicts for claim_id(s): {missing}")

    return [by_id[c.claim_id] for c in claims]


def hallucination_rate(verdicts: list[Verdict]) -> float | None:
    if not verdicts:
        return None
    return sum(1 for v in verdicts if not v.supported) / len(verdicts)


@dataclass(frozen=True, slots=True)
class HumanRating:
    claim_id: str
    supported: bool


def load_human_ratings(data: object) -> list[HumanRating]:
    """Loads the human-rating file format documented in
    docs/eval/annotation_guide.md: a JSON list of
    `{"claim_id": "...", "supported": true|false}` objects, one per claim a
    human reviewer scored on the same rubric the judge model used."""
    if not isinstance(data, list):
        raise ValueError("human ratings file must be a JSON list")
    ratings = []
    for entry in data:
        if not isinstance(entry, dict) or "claim_id" not in entry or "supported" not in entry:
            raise ValueError(f"malformed human rating entry: {entry!r}")
        ratings.append(
            HumanRating(claim_id=str(entry["claim_id"]), supported=bool(entry["supported"]))
        )
    return ratings


@dataclass(frozen=True, slots=True)
class AgreementResult:
    n_compared: int
    agreement_rate: float
    cohens_kappa: float | None
    """None when both raters agree on every item (or one rater is constant)
    — kappa's chance-correction term is undefined/zero-denominator there,
    not a computation bug."""


def inter_rater_agreement(
    judge_verdicts: list[Verdict], human_ratings: list[HumanRating]
) -> AgreementResult:
    """Compares judge verdicts against human ratings for whichever
    claim_ids appear in both — claude_context.md decision #3's third
    mitigation (human-verified subset) applied to the hallucination judge
    specifically, since ADR-0013's circularity concern applies just as much
    to a judge scoring its own kind of output.
    """
    judge_by_id = {v.claim_id: v.supported for v in judge_verdicts}
    human_by_id = {r.claim_id: r.supported for r in human_ratings}
    shared_ids = sorted(set(judge_by_id) & set(human_by_id))

    if not shared_ids:
        return AgreementResult(n_compared=0, agreement_rate=0.0, cohens_kappa=None)

    agreements = sum(1 for cid in shared_ids if judge_by_id[cid] == human_by_id[cid])
    n = len(shared_ids)
    agreement_rate = agreements / n

    p_judge_true = sum(1 for cid in shared_ids if judge_by_id[cid]) / n
    p_human_true = sum(1 for cid in shared_ids if human_by_id[cid]) / n
    p_expected = p_judge_true * p_human_true + (1 - p_judge_true) * (1 - p_human_true)

    kappa: float | None
    if p_expected >= 1.0:  # noqa: SIM108 - the ternary form doesn't fit the line-length limit
        kappa = None
    else:
        kappa = (agreement_rate - p_expected) / (1 - p_expected)

    return AgreementResult(n_compared=n, agreement_rate=agreement_rate, cohens_kappa=kappa)


__all__ = [
    "AgreementResult",
    "Claim",
    "HallucinationJudgeError",
    "HumanRating",
    "Verdict",
    "claims_from_note",
    "hallucination_rate",
    "inter_rater_agreement",
    "judge_claims",
    "load_human_ratings",
]
