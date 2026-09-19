"""Runs the real single-pass baseline extraction (Phase 4's frozen control
arm) against every gold-annotated consultation and scores it (Phase 5) —
`coda-eval run-baseline-eval`. This is the "these are the numbers GoT must
beat" run.

Reuses nlp-service's real extraction/summary code (`nlp_service.extraction.
run_extraction`, `nlp_service.summary.run_summary`, `nlp_service.db.
write_pipeline_outputs`) directly — the same functions the real pipeline's
`STAGE_NLP` handler calls (`nlp_service/worker.py`) — rather than
reimplementing extraction logic here, so this measures the actual baseline
arm, not a parallel approximation of it. This mirrors `cli.py`'s
`run-primock57-asr`, which calls `asr_service` directly rather than
reimplementing ASR.

**Not built here**: Phase 5's full resumable, quota-pausing multi-day
runner. This command runs a bounded item set once, with a light retry-with-
backoff on `QUOTA_EXHAUSTED` (a handful of consultations, not a multi-day
sweep) — the harder resumability requirement (checkpoint after each
consultation, survive kill/restart, pause across a daily quota reset) is a
separate, larger piece of work, honestly left open rather than half-built.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

import psycopg

from coda_eval import db, gold
from coda_eval.metrics import field_scoring, summary_metrics
from coda_eval.metrics.hallucination import (
    Claim,
    Verdict,
    claims_from_note,
    hallucination_rate,
    judge_claims,
)
from coda_eval.note_convert import note_to_fields_dict
from coda_eval.registry import ManifestEntry
from coda_eval.retry import complete_with_quota_retry
from coda_worker_sdk.errors import FatalError
from nlp_service import db as nlp_db
from nlp_service import prompts as nlp_prompts
from nlp_service.extraction import run_extraction
from nlp_service.llm.client import LLMClient
from nlp_service.summary import run_summary

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


@dataclass(slots=True)
class ConsultationResult:
    item_id: str
    session_id: str
    language: str
    schema_valid: bool = False
    repair_attempts: int = 0
    extraction_tokens_in: int = 0
    extraction_tokens_out: int = 0
    summary_tokens_in: int = 0
    summary_tokens_out: int = 0
    wall_ms: int = 0
    field_counts: dict[str, field_scoring.FieldCounts] = field(default_factory=dict)
    rouge_l: summary_metrics.RougeLResult | None = None
    bertscore: summary_metrics.BertScoreResult | None = None
    hyp_summary: str = ""
    gold_summary: str = ""
    claims: list[Claim] = field(default_factory=list)
    verdicts: list[Verdict] = field(default_factory=list)
    error: str | None = None
    """Set (and every other field left at its default) if extraction itself
    failed — schema_valid=False is still meaningful in that case (it's what
    FatalError('EXTRACTION_SCHEMA_INVALID') means), everything downstream of
    a note that was never produced is simply not scored for this item."""


async def run_one_consultation(
    *,
    item_id: str,
    session_id: str,
    gold_data: dict[str, object],
    turns: list[gold.ReferenceTurn],
    llm_client: LLMClient,
    async_conn: psycopg.AsyncConnection,
    base_model: str,
    judge_model: str,
    consultation_id: str,
    run_config_id: str,
    repair_max_attempts: int,
    timeout_s: float,
    language: str,
) -> ConsultationResult:
    result = ConsultationResult(item_id=item_id, session_id=session_id, language=language)
    started = time.monotonic()

    transcript_turns_text = gold.transcript_turns_text(turns)
    known_turn_ids = {t.turn_id for t in turns}
    all_turn_ids = sorted(known_turn_ids)

    try:
        extraction = await complete_with_quota_retry(
            lambda: run_extraction(
                conn=async_conn,
                llm_client=llm_client,
                model=base_model,
                transcript_turns_text=transcript_turns_text,
                known_turn_ids=known_turn_ids,
                consultation_id=consultation_id,
                run_config_id=run_config_id,
                repair_max_attempts=repair_max_attempts,
                timeout_s=timeout_s,
                language=language,
            ),
            description=f"{item_id} extraction",
        )
    except FatalError as exc:
        result.error = f"extraction failed: {exc}"
        result.wall_ms = int((time.monotonic() - started) * 1000)
        return result

    result.schema_valid = extraction.schema_valid
    result.repair_attempts = extraction.repair_attempts
    result.extraction_tokens_in = extraction.tokens_in
    result.extraction_tokens_out = extraction.tokens_out

    try:
        summary = await complete_with_quota_retry(
            lambda: run_summary(
                conn=async_conn,
                llm_client=llm_client,
                model=base_model,
                transcript_turns_text=transcript_turns_text,
                min_chars=20,
                timeout_s=timeout_s,
                language=language,
            ),
            description=f"{item_id} summary",
        )
    except FatalError as exc:
        result.error = f"summary failed: {exc}"
        result.wall_ms = int((time.monotonic() - started) * 1000)
        return result

    result.summary_tokens_in = summary.tokens_in
    result.summary_tokens_out = summary.tokens_out
    result.hyp_summary = summary.text
    result.gold_summary = str(gold_data["summary"])

    await nlp_db.write_pipeline_outputs(
        async_conn,
        consultation_id=consultation_id,
        run_config_id=run_config_id,
        note=extraction.note,
        summary_text=summary.text,
    )

    hyp_fields = note_to_fields_dict(extraction.note)
    gold_fields = gold_data["fields"]
    assert isinstance(gold_fields, dict)
    result.field_counts = field_scoring.score_consultation(gold_fields, hyp_fields)

    result.rouge_l = summary_metrics.rouge_l(result.gold_summary, result.hyp_summary)

    result.claims = claims_from_note(hyp_fields, result.hyp_summary, all_turn_ids=all_turn_ids)
    try:
        result.verdicts = await complete_with_quota_retry(
            lambda: judge_claims(
                llm_client=llm_client,
                model=judge_model,
                turns=turns,
                claims=result.claims,
                timeout_s=timeout_s,
            ),
            description=f"{item_id} hallucination judge",
        )
    except Exception as exc:  # noqa: BLE001 - judge failure shouldn't sink the whole item
        logger.error("hallucination judge failed for %s: %s", item_id, exc)

    result.wall_ms = int((time.monotonic() - started) * 1000)
    return result


def load_gold_items(gold_dir: Path) -> list[tuple[str, dict[str, object]]]:
    """Returns (session_id, gold_data) for every *validated* gold file under
    `gold_dir` — a malformed gold file is skipped with a logged error, never
    silently scored against.
    """
    from coda_eval.gold_schema import validate_gold_file

    items = []
    for p in sorted(gold_dir.glob("*.gold.json")):
        result = validate_gold_file(p)
        if not result.valid:
            logger.error("skipping invalid gold file %s: %s", p, result.errors)
            continue
        assert result.parsed is not None
        session_id = str(result.parsed["session_id"])
        items.append((session_id, result.parsed))
    return items


@dataclass(frozen=True, slots=True)
class BaselineEvalSummary:
    n_items: int
    n_schema_valid: int
    n_errors: int
    field_scores: field_scoring.FieldScoringResult
    rouge_l_avg: summary_metrics.RougeLResult | None
    bertscore_avg: summary_metrics.BertScoreResult | None
    hallucination_rate_overall: float | None
    total_tokens_in: int
    total_tokens_out: int
    total_wall_ms: int
    base_model: str
    judge_model: str
    prompt_set_hash: str
    git_sha: str


async def run_baseline_eval(
    *,
    gold_dir: Path,
    manifest_entries: Mapping[str, ManifestEntry],
    llm_client: LLMClient,
    async_conn: psycopg.AsyncConnection,
    sync_conn: psycopg.Connection,
    base_model: str,
    judge_model: str,
    repair_max_attempts: int = 2,
    timeout_s: float = 90.0,
    language: str = nlp_prompts.DEFAULT_LANGUAGE,
    limit: int | None = None,
) -> tuple[list[ConsultationResult], BaselineEvalSummary, str, str]:
    """Runs every gold-annotated item, persists eval_results, and returns
    (per-item results, aggregate summary, run_config_id, eval_run_id).

    `sync_conn` is not optional — unlike `run-primock57-asr`'s WER/CER path
    (a pure measurement with no FK target), `nlp_service.db.
    write_pipeline_outputs` unconditionally needs a real `consultation_id`
    to satisfy `extractions`/`clinical_notes`' foreign keys, the same as the
    live pipeline always has Postgres. There is no partial "skip the DB"
    mode for this command — only whether the aggregate `eval_results` rows
    are ALSO written (they always are, alongside the per-item pipeline
    tables every real run produces).
    """
    items = load_gold_items(gold_dir)
    if limit is not None:
        items = items[:limit]

    config = {
        "eval_kind": "baseline_extraction",
        "base_model": base_model,
        "judge_model": judge_model,
        "got_enabled": False,
        "prompt_set_hash": nlp_prompts.prompt_set_hash(language=language),
        "language": language,
        "git_sha": db.git_sha(Path(__file__).resolve().parents[4]),
    }
    run_config_id = db.intern_run_config(sync_conn, config=config, arm="baseline", schema_version=1)
    eval_run_id = db.create_eval_run(
        sync_conn,
        name=f"baseline-extraction-{db.canonical_content_hash(config)[:8]}",
        dataset_split="all",
        arm="baseline",
        run_config_id=run_config_id,
    )
    sync_conn.commit()

    results: list[ConsultationResult] = []
    for session_id, gold_data in items:
        item_id = str(gold_data["item_id"])
        entry = manifest_entries.get(session_id)
        if entry is None or entry.reference_transcript_path is None:
            logger.error("no manifest entry for gold session_id=%s, skipping", session_id)
            continue
        turns = gold.parse_reference_transcript(Path(entry.reference_transcript_path))

        ref = db.ensure_dataset_item_consultation(
            sync_conn, item_id=item_id, language=language, licence=entry.licence
        )
        consultation_id = ref.consultation_id
        sync_conn.commit()

        logger.info("running baseline extraction: %s", item_id)
        result = await run_one_consultation(
            item_id=item_id,
            session_id=session_id,
            gold_data=gold_data,
            turns=turns,
            llm_client=llm_client,
            async_conn=async_conn,
            base_model=base_model,
            judge_model=judge_model,
            consultation_id=consultation_id,
            run_config_id=run_config_id,
            repair_max_attempts=repair_max_attempts,
            timeout_s=timeout_s,
            language=language,
        )
        results.append(result)
        if result.error:
            logger.error("%s: %s", item_id, result.error)
        else:
            logger.info(
                "%s: schema_valid=%s repair_attempts=%d hallucination_rate=%s wall_ms=%d",
                item_id, result.schema_valid, result.repair_attempts,
                hallucination_rate(result.verdicts), result.wall_ms,
            )

    # BERTScore batched across every consultation that produced a summary —
    # loading distilbert-base-uncased once, not once per item.
    scored = [r for r in results if not r.error]
    if scored:
        bert_results = summary_metrics.bertscore_batch(
            [r.gold_summary for r in scored], [r.hyp_summary for r in scored]
        )
        for r, b in zip(scored, bert_results, strict=True):
            r.bertscore = b

    summary = _summarize(
        results, base_model=base_model, judge_model=judge_model, language=language
    )

    _write_eval_results(
        sync_conn, results,
        eval_run_id=eval_run_id, run_config_id=run_config_id, language=language,
    )
    db.finish_eval_run(
        sync_conn, eval_run_id=eval_run_id, status="completed",
        total_tokens=summary.total_tokens_in + summary.total_tokens_out,
    )
    sync_conn.commit()

    return results, summary, run_config_id, eval_run_id


def _summarize(
    results: list[ConsultationResult], *, base_model: str, judge_model: str, language: str
) -> BaselineEvalSummary:
    scored = [r for r in results if not r.error]
    field_results = field_scoring.aggregate([r.field_counts for r in scored if r.field_counts])

    rouge_values = [r.rouge_l for r in scored if r.rouge_l is not None]
    rouge_avg = None
    if rouge_values and len(rouge_values) == len(scored):
        n = len(rouge_values)
        rouge_avg = summary_metrics.RougeLResult(
            precision=sum(rv.precision for rv in rouge_values) / n,
            recall=sum(rv.recall for rv in rouge_values) / n,
            fmeasure=sum(rv.fmeasure for rv in rouge_values) / n,
        )

    bert_values = [r.bertscore for r in scored if r.bertscore is not None]
    bert_avg = None
    if bert_values:
        n = len(bert_values)
        bert_avg = summary_metrics.BertScoreResult(
            precision=sum(bv.precision for bv in bert_values) / n,
            recall=sum(bv.recall for bv in bert_values) / n,
            f1=sum(bv.f1 for bv in bert_values) / n,
            model=bert_values[0].model,
        )

    all_verdicts = [v for r in scored for v in r.verdicts]

    return BaselineEvalSummary(
        n_items=len(results),
        n_schema_valid=sum(1 for r in results if r.schema_valid),
        n_errors=sum(1 for r in results if r.error),
        field_scores=field_results,
        rouge_l_avg=rouge_avg,
        bertscore_avg=bert_avg,
        hallucination_rate_overall=hallucination_rate(all_verdicts),
        total_tokens_in=sum(r.extraction_tokens_in + r.summary_tokens_in for r in results),
        total_tokens_out=sum(r.extraction_tokens_out + r.summary_tokens_out for r in results),
        total_wall_ms=sum(r.wall_ms for r in results),
        base_model=base_model,
        judge_model=judge_model,
        prompt_set_hash=nlp_prompts.prompt_set_hash(language=language),
        git_sha=db.git_sha(Path(__file__).resolve().parents[4]),
    )


def _write_eval_results(
    conn: psycopg.Connection,
    results: list[ConsultationResult],
    *,
    eval_run_id: str,
    run_config_id: str,
    language: str,
) -> None:
    for r in results:
        if r.error or not r.field_counts:
            continue
        # Per-item consultation_id was ensured during the run; look it up
        # again rather than threading it through ConsultationResult, since
        # eval_results needs it and nothing else in this dataclass does.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM consent_records WHERE subject_ref = %s", (r.item_id,)
            )
            row = cur.fetchone()
        if row is None:
            continue
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM consultations WHERE consent_record_id = %s", (row[0],))
            crow = cur.fetchone()
        if crow is None:
            continue
        consultation_id = str(crow[0])

        def w(
            metric_key: str, value: float | None, *, _consultation_id: str = consultation_id
        ) -> None:
            if value is None:
                return
            db.write_eval_result(
                conn,
                eval_run_id=eval_run_id,
                consultation_id=_consultation_id,
                run_config_id=run_config_id,
                metric_key=metric_key,
                metric_value=value,
                language=language,
                reference_provenance="llm_silver",
            )

        w("schema_valid", 1.0 if r.schema_valid else 0.0)
        w("repair_attempts", float(r.repair_attempts))
        w("hallucination_rate", hallucination_rate(r.verdicts))
        if r.rouge_l:
            w("rouge_l_f1", r.rouge_l.fmeasure)
        if r.bertscore:
            w("bertscore_f1", r.bertscore.f1)
        for field_name, counts in r.field_counts.items():
            w(f"field_f1__{field_name}", counts.f1())
    conn.commit()


def write_baseline_report(
    results: list[ConsultationResult], summary: BaselineEvalSummary, path: Path
) -> None:
    from datetime import UTC, datetime

    n = summary.n_items
    if n:
        schema_valid_line = (
            f"- Schema-valid JSON: {summary.n_schema_valid}/{n} "
            f"({100 * summary.n_schema_valid / n:.1f}%)"
        )
        total_tok = summary.total_tokens_in + summary.total_tokens_out
        tokens_line = (
            f"- Total tokens: {summary.total_tokens_in} in / {summary.total_tokens_out} out "
            f"over {n} consultations ({total_tok / n:.0f} avg/consultation)"
        )
        wall_line = (
            f"- Total wall-clock: {summary.total_wall_ms / 1000:.1f}s "
            f"({summary.total_wall_ms / n / 1000:.1f}s avg/consultation)"
        )
    else:
        schema_valid_line = "- No items run"
        tokens_line = ""
        wall_line = ""

    if summary.hallucination_rate_overall is not None:
        hallucination_line = (
            "- Hallucination rate (LLM-judge, all field + summary claims): "
            f"{summary.hallucination_rate_overall:.4f}"
        )
    else:
        hallucination_line = "- Hallucination rate: not computed (no claims judged)"

    no_judge_items = [r.item_id for r in results if not r.error and not r.verdicts]
    caveats = []
    if no_judge_items:
        caveats.append(
            f"Hallucination rate was not computed for {len(no_judge_items)} of {n} "
            f"consultation(s) ({', '.join(no_judge_items)}) — the judge request still exceeded "
            "Groq's free-tier `openai/gpt-oss-20b` TPM cap (8000 tokens/minute) even after "
            "restricting to only cited turns (`coda_eval.metrics.hallucination`'s module "
            "docstring) and the built-in retry-with-backoff. Every other metric in this report "
            "for that item is real. The aggregate hallucination rate above is computed only over "
            "items that WERE judged, never treating a missing judgment as either supported or "
            "unsupported."
        )
    caveats.append(
        "Reference labels (`data/gold/primock57/*.gold.json`) are `llm_silver` — a first-pass "
        "mapping of PriMock57's real clinician notes by Claude Sonnet 5, not yet hand-corrected "
        "by a human reviewer. Decision #3's ~15-consultation human-verified subset, and the "
        "inter-rater agreement check against it (`coda_eval.metrics.hallucination."
        "inter_rater_agreement`), are both follow-up work, not done this run."
    )

    lines = [
        "# Baseline (single-pass extraction) evaluation report",
        "",
        "**These are the numbers the GoT pipeline (Phase 6) must beat.** Frozen control arm "
        "(claude_context.md decision #11), same base model held constant across every arm.",
        "",
        "**English-only.** No multilingual (Kannada-English) evaluation has been performed — "
        "v1 is English-only by design (claude_context.md §2.1).",
        "",
        "## Run metadata",
        "",
        f"- Timestamp (UTC): {datetime.now(UTC).isoformat()}",
        f"- Git SHA: `{summary.git_sha}`",
        f"- Base model (system under test): `{summary.base_model}`",
        f"- Hallucination judge model: `{summary.judge_model}`",
        f"- Prompt set hash: `{summary.prompt_set_hash}`",
        f"- Dataset: PriMock57, {n} gold-annotated consultations "
        "(the 8-of-57 subset fetched — decision #61)",
        "- Reference label provenance: `llm_silver` — first-pass gold mapping by Claude "
        "Sonnet 5 (a different model than the system under test, decision #3/ADR-0013), "
        "**not yet hand-corrected** by the project owner (decision #3's ~15-consultation "
        "human-verified subset is a follow-up step, not done this run)",
        "",
        "## Caveats",
        "",
        *[f"- {c}" for c in caveats],
        "",
        "## Headline numbers",
        "",
        schema_valid_line,
        f"- Extraction/summary errors (repair budget exhausted or summary too short): "
        f"{summary.n_errors}/{n}",
        hallucination_line,
        tokens_line,
        wall_line,
        "",
    ]

    if summary.rouge_l_avg:
        rl = summary.rouge_l_avg
        lines += [
            "## Summarization metrics (vs. gold summary)",
            "",
            "| Metric | Precision | Recall | F1 |",
            "|---|---|---|---|",
            f"| ROUGE-L | {rl.precision:.4f} | {rl.recall:.4f} | {rl.fmeasure:.4f} |",
        ]
        if summary.bertscore_avg:
            b = summary.bertscore_avg
            lines.append(
                f"| BERTScore (`{b.model}`) | {b.precision:.4f} | {b.recall:.4f} | {b.f1:.4f} |"
            )
        lines.append("")

    lines += [
        "## Field-level precision / recall / F1",
        "",
        "Matching rule per field documented in `docs/eval/annotation_guide.md` §5. Aggregated as "
        "total edits over total opportunities across all consultations (claude_context.md "
        "decision #63's rule), never a mean of per-consultation ratios.",
        "",
        "| Field | TP | FP | FN | TN | Precision | Recall | F1 | Empty rate | "
        "Hallucinated rate |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for field_name in field_scoring.ALL_FIELDS:
        c = summary.field_scores.per_field.get(field_name, field_scoring.FieldCounts())
        lines.append(
            f"| {field_name} | {c.tp} | {c.fp} | {c.fn} | {c.tn} | {_fmt(c.precision())} | "
            f"{_fmt(c.recall())} | {_fmt(c.f1())} | {_fmt(c.empty_rate())} | "
            f"{_fmt(c.hallucination_rate())} |"
        )
    overall = summary.field_scores.overall
    lines.append(
        f"| **overall (micro)** | {overall.tp} | {overall.fp} | {overall.fn} | {overall.tn} | "
        f"{_fmt(overall.precision())} | {_fmt(overall.recall())} | {_fmt(overall.f1())} | "
        f"{_fmt(overall.empty_rate())} | {_fmt(overall.hallucination_rate())} |"
    )
    lines.append("")

    lines += [
        "## Per-consultation detail",
        "",
        "| Item | Schema valid | Repair attempts | Hallucination rate | Wall (ms) | Error |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        h = hallucination_rate(r.verdicts)
        lines.append(
            f"| {r.item_id} | {r.schema_valid} | {r.repair_attempts} | {_fmt(h)} | "
            f"{r.wall_ms} | {r.error or ''} |"
        )
    lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def write_baseline_report_json(
    results: list[ConsultationResult], summary: BaselineEvalSummary, path: Path
) -> None:
    """The machine-readable counterpart to `write_baseline_report`'s
    markdown table (plan.md Phase 5's "results emitted as both machine-
    readable JSON and a markdown table") — the same numbers, structured for
    a script to consume rather than a human to read. `eval_results` in
    Postgres remains the authoritative, queryable record (ADR-0012); this
    file is a portable snapshot of one run, not a second source of truth.
    """
    import json as json_module
    from datetime import UTC, datetime

    data = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "git_sha": summary.git_sha,
        "base_model": summary.base_model,
        "judge_model": summary.judge_model,
        "prompt_set_hash": summary.prompt_set_hash,
        "n_items": summary.n_items,
        "n_schema_valid": summary.n_schema_valid,
        "n_errors": summary.n_errors,
        "hallucination_rate_overall": summary.hallucination_rate_overall,
        "total_tokens_in": summary.total_tokens_in,
        "total_tokens_out": summary.total_tokens_out,
        "total_wall_ms": summary.total_wall_ms,
        "rouge_l": (
            {
                "precision": summary.rouge_l_avg.precision,
                "recall": summary.rouge_l_avg.recall,
                "fmeasure": summary.rouge_l_avg.fmeasure,
            }
            if summary.rouge_l_avg
            else None
        ),
        "bertscore": (
            {
                "precision": summary.bertscore_avg.precision,
                "recall": summary.bertscore_avg.recall,
                "f1": summary.bertscore_avg.f1,
                "model": summary.bertscore_avg.model,
            }
            if summary.bertscore_avg
            else None
        ),
        "fields": {
            field_name: {
                "tp": c.tp, "fp": c.fp, "fn": c.fn, "tn": c.tn,
                "precision": c.precision(), "recall": c.recall(), "f1": c.f1(),
                "empty_rate": c.empty_rate(), "hallucination_rate": c.hallucination_rate(),
            }
            for field_name, c in summary.field_scores.per_field.items()
        },
        "overall_field_scores": {
            "tp": summary.field_scores.overall.tp,
            "fp": summary.field_scores.overall.fp,
            "fn": summary.field_scores.overall.fn,
            "tn": summary.field_scores.overall.tn,
            "precision": summary.field_scores.overall.precision(),
            "recall": summary.field_scores.overall.recall(),
            "f1": summary.field_scores.overall.f1(),
        },
        "consultations": [
            {
                "item_id": r.item_id,
                "language": r.language,
                "schema_valid": r.schema_valid,
                "repair_attempts": r.repair_attempts,
                "hallucination_rate": hallucination_rate(r.verdicts),
                "n_claims_judged": len(r.verdicts),
                # Per-item metrics, not just aggregates — the paired
                # comparison (coda_eval.compare) needs one value per
                # consultation per arm to bootstrap a confidence interval on
                # the arm-to-arm difference. Aggregates alone cannot support
                # that: a mean of 8 numbers carries no information about the
                # spread across those 8 items.
                "field_micro_f1": item_micro_f1(r.field_counts),
                "rouge_l_f1": r.rouge_l.fmeasure if r.rouge_l else None,
                "bertscore_f1": r.bertscore.f1 if r.bertscore else None,
                "extraction_tokens_in": r.extraction_tokens_in,
                "extraction_tokens_out": r.extraction_tokens_out,
                "summary_tokens_in": r.summary_tokens_in,
                "summary_tokens_out": r.summary_tokens_out,
                "total_tokens": (
                    r.extraction_tokens_in
                    + r.extraction_tokens_out
                    + r.summary_tokens_in
                    + r.summary_tokens_out
                ),
                "wall_ms": r.wall_ms,
                "error": r.error,
            }
            for r in results
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json_module.dumps(data, indent=2), encoding="utf-8")


def item_micro_f1(field_counts: dict[str, field_scoring.FieldCounts]) -> float | None:
    """One consultation's field-level micro F1 — the per-item value the
    paired ablation comparison bootstraps over. Shared with `got_eval.py`
    rather than duplicated, since both arms produce the same
    `dict[str, FieldCounts]` shape from `field_scoring.score_consultation`.
    """
    if not field_counts:
        return None
    return field_scoring.aggregate([field_counts]).overall.f1()


def _fmt(value: float | None) -> str:
    return "" if value is None else f"{value:.4f}"


__all__ = [
    "BaselineEvalSummary",
    "ConsultationResult",
    "item_micro_f1",
    "load_gold_items",
    "run_baseline_eval",
    "run_one_consultation",
    "write_baseline_report",
    "write_baseline_report_json",
]
