"""Runs the real GoT-lite arm (Phase 6's experimental arm — thought
construction, typed-edge graph assembly, graph-structured context retrieval,
N-candidate generation, rubric scoring, K-iteration refinement, hierarchical
distillation) against every gold-annotated consultation and scores it the
same way `baseline_eval.py` scores the control arm — `coda-eval run-got-eval`.

Mirrors `baseline_eval.py` deliberately: same gold items, same
`field_scoring`/`summary_metrics`/`hallucination` metrics, same
`ConsultationResult` shape (imported, not duplicated) so `compare.py` can
pair the two arms' per-item numbers directly. The only real difference is
what produces the note: `nlp_service.graph.pipeline.build_thought_graph`
(Modules 1-2) followed by `nlp_service.reasoning.pipeline.run_reasoning`
(Modules 5-6), the same two calls `nlp_service/worker.py`'s `_handle_got`
makes in the live pipeline, rather than `run_extraction`/`run_summary`.

Runs on **gold reference transcripts**, not live ASR — same choice
`run-baseline-eval` already made (data/gold/primock57/*.gold.json's
`turns`), for the same reason: it isolates the reasoning-vs-single-pass
comparison from an ASR-noise confound neither arm has been measured against
yet, and it is the only way either arm can be evaluated without an HF_TOKEN
diarization run against real audio. Stated as a limitation in the ablation
report, not hidden.

Because Module 1/2 writes real `transcripts`/`turns` rows (thoughts.turn_id
is a real FK — `nlp_service.graph.store.materialize_transcript`), this
command needs a live MinIO in addition to the live Postgres+Groq
`run-baseline-eval` already requires (`coda_eval.storage.host_storage_client`).
`transcripts.asr_backend = 'dataset_gold'` (migration 000034) records
honestly that no ASR ran for these rows.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import psycopg

from coda.v1 import runconfig_pb2, transcript_pb2
from coda_eval import db, gold
from coda_eval.baseline_eval import ConsultationResult, item_micro_f1
from coda_eval.metrics import field_scoring, summary_metrics
from coda_eval.metrics.hallucination import claims_from_note, hallucination_rate, judge_claims
from coda_eval.note_convert import note_to_fields_dict
from coda_eval.registry import ManifestEntry
from coda_eval.retry import complete_with_quota_retry
from coda_worker_sdk.errors import FatalError
from nlp_service import prompts as nlp_prompts
from nlp_service.graph.pipeline import build_thought_graph
from nlp_service.llm.client import LLMClient
from nlp_service.reasoning.generation import DEFAULT_GENERATION_CONTEXT_TOKENS
from nlp_service.reasoning.pipeline import run_reasoning
from nlp_service.reasoning.scoring import SCORER_HEURISTIC

logger = logging.getLogger(__name__)

GOT_ARM = "got_k2"
"""claude_context.md §6 / architecture.md §6.2's headline experimental arm:
N=3, K=2, graph context on, knowledge augmentation off. The component
ablation (K=1, ±graph, +KG) is plan.md Phase 10's job, not this first
baseline-vs-GoT-lite comparison's."""

_SPEAKER_ROLE_BY_NAME = {
    "doctor": transcript_pb2.SpeakerRole.SPEAKER_ROLE_DOCTOR,
    "patient": transcript_pb2.SpeakerRole.SPEAKER_ROLE_PATIENT,
}


def transcript_from_gold_turns(
    turns: list[gold.ReferenceTurn],
    *,
    consultation_id: str,
    run_config_id: str,
    language: str,
) -> transcript_pb2.Transcript:
    """Builds the `Transcript` proto Module 1/2 needs directly from a gold
    file's reference turns — no ASR, no redaction (both arms read the same
    unredacted reference text `run-baseline-eval` already reads via
    `gold.transcript_turns_text`; text_redacted is set equal to text for the
    same reason that command never runs a real redaction step either)."""
    t = transcript_pb2.Transcript(
        consultation_id=consultation_id,
        run_config_id=run_config_id,
        language=language,
        asr_backend="dataset_gold",
        asr_model="primock57_reference_transcript",
    )
    for turn in turns:
        role = _SPEAKER_ROLE_BY_NAME.get(
            turn.speaker.strip().lower(), transcript_pb2.SpeakerRole.SPEAKER_ROLE_UNKNOWN
        )
        t.turns.add(
            turn_index=turn.turn_id,
            speaker_label=role,
            text=turn.text,
            text_redacted=turn.text,
            confidence=1.0,
        )
    return t


def build_got_run_config(
    *, base_model: str, judge_model: str, structural_model: str, language: str
) -> runconfig_pb2.RunConfig:
    """The `got_k2` row of architecture.md §6.2's ablation matrix, matching
    `go/internal/http/jobs_handlers.go`'s `knownArms["got_k2"]` exactly
    (n_candidates=3, k_iterations=2, graph_context_enabled=true,
    kg_enabled=false) plus `scorer_backend="heuristic"` — the zero-extra-
    API-call scorer, chosen for this first live run to keep quota
    consumption bounded (thought construction + edge prediction + N=3
    generation + K=2 refinement already cost several calls per field before
    any judge call is added)."""
    return runconfig_pb2.RunConfig(
        arm=GOT_ARM,
        schema_version=1,
        base_model=base_model,
        judge_model=judge_model,
        structural_model=structural_model,
        embed_model="all-MiniLM-L6-v2",
        asr_backend="dataset_gold",
        asr_model="primock57_reference_transcript",
        got_enabled=True,
        n_candidates=3,
        k_iterations=2,
        graph_context_enabled=True,
        kg_enabled=False,
        kg_backend="none",
        scorer_weights=runconfig_pb2.ScorerWeights(relevance=0.5, consistency=0.3, redundancy=0.2),
        scorer_backend=SCORER_HEURISTIC,
        generation_context_tokens=DEFAULT_GENERATION_CONTEXT_TOKENS,
        temperature=0.2,
        top_p=0.9,
        seed=42,
        prompt_set_hash=nlp_prompts.prompt_set_hash(language=language),
        # Matches go-api's knownArms["got_k2"] (redaction is structurally
        # unskippable in the real pipeline). This harness runs no redact
        # stage at all — same as run-baseline-eval — because the input is
        # PriMock57's own already-de-identified reference transcript, not
        # live audio; the field is recorded for parity with the real arm's
        # config, not because a redaction step ran here.
        redaction_enabled=True,
    )


async def run_one_consultation_got(
    *,
    item_id: str,
    session_id: str,
    gold_data: dict[str, object],
    turns: list[gold.ReferenceTurn],
    llm_client: LLMClient,
    storage: object,
    async_conn: psycopg.AsyncConnection,
    run_config: runconfig_pb2.RunConfig,
    consultation_id: str,
    run_config_id: str,
    repair_max_attempts: int,
    timeout_s: float,
    language: str,
) -> ConsultationResult:
    result = ConsultationResult(item_id=item_id, session_id=session_id, language=language)
    started = time.monotonic()

    all_turn_ids = sorted(t.turn_id for t in turns)
    transcript = transcript_from_gold_turns(
        turns, consultation_id=consultation_id, run_config_id=run_config_id, language=language
    )

    try:
        build = await complete_with_quota_retry(
            lambda: build_thought_graph(
                conn=async_conn,
                storage=storage,
                llm_client=llm_client,
                transcript=transcript,
                consultation_id=consultation_id,
                run_config_id=run_config_id,
                transcript_uri="dataset_gold://primock57/" + session_id,
                base_model=run_config.base_model,
                structural_model=run_config.structural_model,
                asr_backend="dataset_gold",
                asr_model="primock57_reference_transcript",
                repair_max_attempts=repair_max_attempts,
                timeout_s=timeout_s,
                language=language,
            ),
            description=f"{item_id} graph build",
        )
    except FatalError as exc:
        result.error = f"graph build failed: {exc}"
        result.wall_ms = int((time.monotonic() - started) * 1000)
        return result

    try:
        reasoning = await complete_with_quota_retry(
            lambda: run_reasoning(
                conn=async_conn,
                storage=storage,
                llm_client=llm_client,
                graph=build.graph,
                run_config=run_config,
                consultation_id=consultation_id,
                run_config_id=run_config_id,
                repair_max_attempts=repair_max_attempts,
                timeout_s=timeout_s,
                language=language,
            ),
            description=f"{item_id} reasoning",
        )
    except FatalError as exc:
        result.error = f"reasoning failed: {exc}"
        result.wall_ms = int((time.monotonic() - started) * 1000)
        return result

    # GoT's note is built by rule-based distillation over typed proto values,
    # never raw LLM JSON — there is no parse-and-repair step to fail, so
    # "schema valid" is unconditionally true the same way worker.py's
    # `_handle_got` reports it. `repair_attempts` still carries real
    # information: JSON repair inside thought construction / candidate
    # generation's own structured-output calls.
    result.schema_valid = True
    result.repair_attempts = build.repair_attempts + reasoning.repair_attempts
    # GoT has no separate "extraction call" vs "summary call" split the way
    # the baseline arm does — generation/scoring/refinement/summary are all
    # folded into one `run_reasoning` cost. Recorded as extraction_tokens_*
    # (graph build) and summary_tokens_* (reasoning, which itself includes
    # the summary generation call) purely so both arms' JSON reports share
    # one column layout; `compare.py` sums both columns per item anyway.
    result.extraction_tokens_in = build.tokens_in
    result.extraction_tokens_out = build.tokens_out
    result.summary_tokens_in = reasoning.tokens_in
    result.summary_tokens_out = reasoning.tokens_out
    result.hyp_summary = reasoning.summary_text
    result.gold_summary = str(gold_data["summary"])

    hyp_fields = note_to_fields_dict(reasoning.note)
    gold_fields = gold_data["fields"]
    assert isinstance(gold_fields, dict)
    result.field_counts = field_scoring.score_consultation(gold_fields, hyp_fields)

    result.rouge_l = summary_metrics.rouge_l(result.gold_summary, result.hyp_summary)

    result.claims = claims_from_note(hyp_fields, result.hyp_summary, all_turn_ids=all_turn_ids)
    try:
        result.verdicts = await complete_with_quota_retry(
            lambda: judge_claims(
                llm_client=llm_client,
                model=run_config.judge_model,
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


@dataclass(frozen=True, slots=True)
class GotEvalSummary:
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
    structural_model: str
    n_candidates: int
    k_iterations: int
    graph_context_enabled: bool
    scorer_backend: str
    prompt_set_hash: str
    git_sha: str


async def run_got_eval(
    *,
    gold_dir: Path,
    manifest_entries: dict[str, ManifestEntry],
    llm_client: LLMClient,
    storage: object,
    async_conn: psycopg.AsyncConnection,
    sync_conn: psycopg.Connection,
    base_model: str,
    judge_model: str,
    structural_model: str,
    repair_max_attempts: int = 2,
    timeout_s: float = 120.0,
    language: str = nlp_prompts.DEFAULT_LANGUAGE,
    limit: int | None = None,
) -> tuple[list[ConsultationResult], GotEvalSummary, str, str]:
    """Runs every gold-annotated item through the got_k2 arm, persists
    eval_results, and returns (per-item results, aggregate summary,
    run_config_id, eval_run_id). Mirrors `baseline_eval.run_baseline_eval`
    structurally; see that function's docstring for why `sync_conn` is not
    optional (the same `consultation_id` FK requirement applies here).
    """
    from coda_eval.baseline_eval import load_gold_items

    items = load_gold_items(gold_dir)
    if limit is not None:
        items = items[:limit]

    run_config = build_got_run_config(
        base_model=base_model,
        judge_model=judge_model,
        structural_model=structural_model,
        language=language,
    )
    config = {
        "eval_kind": "got_reasoning",
        "arm": GOT_ARM,
        "base_model": base_model,
        "judge_model": judge_model,
        "structural_model": structural_model,
        "embed_model": run_config.embed_model,
        "got_enabled": True,
        "n_candidates": run_config.n_candidates,
        "k_iterations": run_config.k_iterations,
        "graph_context_enabled": run_config.graph_context_enabled,
        "kg_enabled": run_config.kg_enabled,
        "scorer_backend": run_config.scorer_backend,
        "scorer_weights": {
            "relevance": run_config.scorer_weights.relevance,
            "consistency": run_config.scorer_weights.consistency,
            "redundancy": run_config.scorer_weights.redundancy,
        },
        "generation_context_tokens": run_config.generation_context_tokens,
        "prompt_set_hash": run_config.prompt_set_hash,
        "language": language,
        "git_sha": db.git_sha(Path(__file__).resolve().parents[4]),
    }
    run_config_id = db.intern_run_config(sync_conn, config=config, arm=GOT_ARM, schema_version=1)
    eval_run_id = db.create_eval_run(
        sync_conn,
        name=f"got-reasoning-{db.canonical_content_hash(config)[:8]}",
        dataset_split="all",
        arm=GOT_ARM,
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

        logger.info("running got_k2 reasoning: %s", item_id)
        result = await run_one_consultation_got(
            item_id=item_id,
            session_id=session_id,
            gold_data=gold_data,
            turns=turns,
            llm_client=llm_client,
            storage=storage,
            async_conn=async_conn,
            run_config=run_config,
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
                "%s: hallucination_rate=%s wall_ms=%d",
                item_id, hallucination_rate(result.verdicts), result.wall_ms,
            )

    scored = [r for r in results if not r.error]
    if scored:
        bert_results = summary_metrics.bertscore_batch(
            [r.gold_summary for r in scored], [r.hyp_summary for r in scored]
        )
        for r, b in zip(scored, bert_results, strict=True):
            r.bertscore = b

    summary = _summarize(
        results,
        base_model=base_model,
        judge_model=judge_model,
        structural_model=structural_model,
        run_config=run_config,
        language=language,
    )

    _write_eval_results(
        sync_conn, results, eval_run_id=eval_run_id, run_config_id=run_config_id, language=language
    )
    db.finish_eval_run(
        sync_conn, eval_run_id=eval_run_id, status="completed",
        total_tokens=summary.total_tokens_in + summary.total_tokens_out,
    )
    sync_conn.commit()

    return results, summary, run_config_id, eval_run_id


def _summarize(
    results: list[ConsultationResult],
    *,
    base_model: str,
    judge_model: str,
    structural_model: str,
    run_config: runconfig_pb2.RunConfig,
    language: str,
) -> GotEvalSummary:
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

    return GotEvalSummary(
        n_items=len(results),
        n_schema_valid=sum(1 for r in results if r.schema_valid),
        n_errors=sum(1 for r in results if r.error),
        field_scores=field_results,
        rouge_l_avg=rouge_avg,
        bertscore_avg=bert_avg,
        hallucination_rate_overall=hallucination_rate(all_verdicts),
        total_tokens_in=sum(
            r.extraction_tokens_in + r.summary_tokens_in for r in results
        ),
        total_tokens_out=sum(
            r.extraction_tokens_out + r.summary_tokens_out for r in results
        ),
        total_wall_ms=sum(r.wall_ms for r in results),
        base_model=base_model,
        judge_model=judge_model,
        structural_model=structural_model,
        n_candidates=run_config.n_candidates,
        k_iterations=run_config.k_iterations,
        graph_context_enabled=run_config.graph_context_enabled,
        scorer_backend=run_config.scorer_backend,
        prompt_set_hash=run_config.prompt_set_hash,
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
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM consent_records WHERE subject_ref = %s", (r.item_id,))
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


def write_got_report(results: list[ConsultationResult], summary: GotEvalSummary, path: Path) -> None:
    from datetime import UTC, datetime

    n = summary.n_items
    if n:
        tokens_line = (
            f"- Total tokens: {summary.total_tokens_in} in / {summary.total_tokens_out} out "
            f"over {n} consultations ({(summary.total_tokens_in + summary.total_tokens_out) / n:.0f} "
            "avg/consultation)"
        )
        wall_line = (
            f"- Total wall-clock: {summary.total_wall_ms / 1000:.1f}s "
            f"({summary.total_wall_ms / n / 1000:.1f}s avg/consultation)"
        )
    else:
        tokens_line = ""
        wall_line = ""

    hallucination_line = (
        f"- Hallucination rate (LLM-judge, all field + summary claims): "
        f"{summary.hallucination_rate_overall:.4f}"
        if summary.hallucination_rate_overall is not None
        else "- Hallucination rate: not computed (no claims judged)"
    )

    no_judge_items = [r.item_id for r in results if not r.error and not r.verdicts]
    caveats = [
        "Runs on gold reference transcripts, not live ASR output — the same choice "
        "`run-baseline-eval` makes, so the comparison isolates reasoning from an ASR-noise "
        "confound neither arm has been measured against yet (see docs/eval/got_ablation_v1.md).",
        f"scorer_backend=`{summary.scorer_backend}` — the zero-extra-API-call scorer "
        "(local MiniLM consistency + entity-overlap relevance + n-gram redundancy), not the "
        "LLM-judge relevance rubric, chosen to bound Groq quota consumption on this first live run.",
        "Reference labels (`data/gold/primock57/*.gold.json`) are `llm_silver`, identical caveat "
        "to the baseline report.",
    ]
    if no_judge_items:
        caveats.append(
            f"Hallucination rate was not computed for {len(no_judge_items)} of {n} "
            f"consultation(s) ({', '.join(no_judge_items)}) — same Groq free-tier TPM cap "
            "as the baseline report. Every other metric for that item is real."
        )

    lines = [
        "# GoT-lite (got_k2) evaluation report",
        "",
        "**English-only.** No multilingual (Kannada-English) evaluation has been performed — "
        "v1 is English-only by design (claude_context.md §2.1).",
        "",
        "## Run metadata",
        "",
        f"- Timestamp (UTC): {datetime.now(UTC).isoformat()}",
        f"- Git SHA: `{summary.git_sha}`",
        f"- Arm: `{GOT_ARM}` — N={summary.n_candidates} candidates, K={summary.k_iterations} "
        f"refinement iterations, graph_context_enabled={summary.graph_context_enabled}, "
        "kg_enabled=false",
        f"- Base model (system under test, same as baseline): `{summary.base_model}`",
        f"- Structural model (edge prediction): `{summary.structural_model}`",
        f"- Hallucination judge model: `{summary.judge_model}`",
        f"- Prompt set hash: `{summary.prompt_set_hash}`",
        f"- Dataset: PriMock57, {n} gold-annotated consultations (the 8-of-57 subset fetched)",
        "",
        "## Caveats",
        "",
        *[f"- {c}" for c in caveats],
        "",
        "## Headline numbers",
        "",
        f"- Schema-valid: {summary.n_schema_valid}/{n} (distillation is rule-based over typed "
        "proto values, not parsed LLM JSON, so this is unconditionally true whenever the arm "
        "completes at all)",
        f"- Graph-build/reasoning errors: {summary.n_errors}/{n}",
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
        "| Item | Field micro-F1 | Hallucination rate | Wall (ms) | Error |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        h = hallucination_rate(r.verdicts)
        f1 = item_micro_f1(r.field_counts)
        lines.append(
            f"| {r.item_id} | {_fmt(f1)} | {_fmt(h)} | {r.wall_ms} | {r.error or ''} |"
        )
    lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def write_got_report_json(results: list[ConsultationResult], summary: GotEvalSummary, path: Path) -> None:
    import json as json_module
    from datetime import UTC, datetime

    data = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "git_sha": summary.git_sha,
        "arm": GOT_ARM,
        "base_model": summary.base_model,
        "judge_model": summary.judge_model,
        "structural_model": summary.structural_model,
        "n_candidates": summary.n_candidates,
        "k_iterations": summary.k_iterations,
        "graph_context_enabled": summary.graph_context_enabled,
        "scorer_backend": summary.scorer_backend,
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


def _fmt(value: float | None) -> str:
    return "" if value is None else f"{value:.4f}"


__all__ = [
    "GOT_ARM",
    "GotEvalSummary",
    "build_got_run_config",
    "run_got_eval",
    "run_one_consultation_got",
    "transcript_from_gold_turns",
    "write_got_report",
    "write_got_report_json",
]
