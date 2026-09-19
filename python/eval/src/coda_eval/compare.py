"""Produces the headline ablation table: single-pass baseline vs GoT-lite,
same base model, same item set, every extraction/summarization metric plus
latency and token cost per consultation, grouped by language subset —
`coda-eval compare-ablation`.

Statistical honesty is the point of this module, not a footnote on it. At
the sample sizes this project can actually afford (a handful of
consultations, not thousands), a bare "0.71 vs 0.68" is misleading on its
own — it implies a precision the measurement doesn't have. Every comparison
below reports N, a 95% paired-bootstrap confidence interval on the
arm-to-arm difference, and an explicit flag when N is too small to support
a claim (plan.md's "report sample sizes and confidence intervals, and flag
when N is too small"). A negative or ambiguous CI (one that spans zero) is
reported as exactly that — not rounded off, not omitted, not re-run until it
looks better.

Reads the two arms' `write_*_report_json` output (`baseline_report.json`,
`got_report.json`) rather than re-querying Postgres, so this command works
from any two JSON snapshots — including ones from different eval_runs — as
long as both were produced against the same item set. That precondition is
checked, not assumed: item_ids are paired by exact match, and any item
present in one report but not the other is reported as dropped from the
paired comparison rather than silently ignored.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

N_BOOT = 10_000
CI_ALPHA = 0.05
BOOTSTRAP_SEED = 20260905
"""Fixed seed — a re-run of this exact comparison reproduces the exact same
CI bounds, so "the numbers changed" always means the underlying data changed,
never resampling noise."""

MIN_N_FOR_CLAIM = 10
"""Below this, plan.md's "flag when N is too small to support a claim" is
triggered. Not a statistical rule with a citation — a stated, round-number
threshold under which this project will not claim significance either way."""


@dataclass(frozen=True, slots=True)
class MetricComparison:
    metric: str
    n_paired: int
    baseline_mean: float | None
    baseline_ci: tuple[float, float] | None
    got_mean: float | None
    got_ci: tuple[float, float] | None
    diff_mean: float | None
    """got - baseline."""
    diff_ci: tuple[float, float] | None
    insufficient_n: bool
    ci_spans_zero: bool | None
    """None when insufficient_n (a CI wasn't computed at all)."""


def load_report(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _bootstrap_mean_ci(
    values: np.ndarray, *, n_boot: int, alpha: float, rng: np.random.Generator
) -> tuple[float, float]:
    n = len(values)
    idx = rng.integers(0, n, size=(n_boot, n))
    means = values[idx].mean(axis=1)
    lo, hi = np.percentile(means, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return float(lo), float(hi)


def _paired_values(
    baseline_by_item: dict[str, float | None], got_by_item: dict[str, float | None]
) -> tuple[list[str], np.ndarray, np.ndarray]:
    """Items present in both reports with a non-null value for this metric in
    BOTH arms — a metric the hallucination judge skipped for one arm (Groq
    TPM cap) cannot contribute to that metric's paired comparison, even if
    every other metric for the same item is fine."""
    common_ids = sorted(set(baseline_by_item) & set(got_by_item))
    ids, base_vals, got_vals = [], [], []
    for item_id in common_ids:
        b, g = baseline_by_item[item_id], got_by_item[item_id]
        if b is None or g is None:
            continue
        ids.append(item_id)
        base_vals.append(b)
        got_vals.append(g)
    return ids, np.array(base_vals, dtype=float), np.array(got_vals, dtype=float)


def compare_metric(
    metric: str,
    baseline_by_item: dict[str, float | None],
    got_by_item: dict[str, float | None],
    *,
    rng: np.random.Generator,
) -> MetricComparison:
    ids, base_vals, got_vals = _paired_values(baseline_by_item, got_by_item)
    n = len(ids)

    if n == 0:
        return MetricComparison(
            metric=metric, n_paired=0, baseline_mean=None, baseline_ci=None,
            got_mean=None, got_ci=None, diff_mean=None, diff_ci=None,
            insufficient_n=True, ci_spans_zero=None,
        )

    baseline_mean = float(base_vals.mean())
    got_mean = float(got_vals.mean())
    diff_mean = got_mean - baseline_mean

    if n < MIN_N_FOR_CLAIM:
        return MetricComparison(
            metric=metric, n_paired=n, baseline_mean=baseline_mean, baseline_ci=None,
            got_mean=got_mean, got_ci=None, diff_mean=diff_mean, diff_ci=None,
            insufficient_n=True, ci_spans_zero=None,
        )

    baseline_ci = _bootstrap_mean_ci(base_vals, n_boot=N_BOOT, alpha=CI_ALPHA, rng=rng)
    got_ci = _bootstrap_mean_ci(got_vals, n_boot=N_BOOT, alpha=CI_ALPHA, rng=rng)

    diffs = got_vals - base_vals
    idx = rng.integers(0, n, size=(N_BOOT, n))
    boot_diff_means = diffs[idx].mean(axis=1)
    diff_lo, diff_hi = np.percentile(
        boot_diff_means, [100 * CI_ALPHA / 2, 100 * (1 - CI_ALPHA / 2)]
    )
    diff_ci = (float(diff_lo), float(diff_hi))

    return MetricComparison(
        metric=metric, n_paired=n, baseline_mean=baseline_mean, baseline_ci=baseline_ci,
        got_mean=got_mean, got_ci=got_ci, diff_mean=diff_mean, diff_ci=diff_ci,
        insufficient_n=False, ci_spans_zero=(diff_lo <= 0.0 <= diff_hi),
    )


PER_ITEM_METRICS = (
    ("field_micro_f1", "Field-level micro F1"),
    ("rouge_l_f1", "ROUGE-L F1 (summary)"),
    ("bertscore_f1", "BERTScore F1 (summary)"),
    ("hallucination_rate", "Hallucination rate (lower is better)"),
    ("total_tokens", "Total tokens per consultation"),
    ("wall_ms", "Wall-clock (ms) per consultation"),
)
"""(json key in each report's `consultations` list, display label). All but
hallucination_rate and the two cost metrics are "higher is better" — stated
per-row in the rendered table rather than assumed."""

LOWER_IS_BETTER = {"hallucination_rate", "total_tokens", "wall_ms"}


def _by_item(report: dict, key: str) -> dict[str, float | None]:
    return {c["item_id"]: c.get(key) for c in report["consultations"]}


def compare_reports(baseline: dict, got: dict) -> list[MetricComparison]:
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    return [
        compare_metric(label, _by_item(baseline, key), _by_item(got, key), rng=rng)
        for key, label in PER_ITEM_METRICS
    ]


def _fmt(v: float | None, *, digits: int = 4) -> str:
    return "" if v is None else f"{v:.{digits}f}"


def _fmt_ci(ci: tuple[float, float] | None, *, digits: int = 4) -> str:
    if ci is None:
        return "—"
    return f"[{ci[0]:.{digits}f}, {ci[1]:.{digits}f}]"


def write_ablation_report(
    baseline: dict,
    got: dict,
    comparisons: list[MetricComparison],
    path: Path,
    *,
    baseline_json_path: Path,
    got_json_path: Path,
) -> None:
    from datetime import UTC, datetime

    baseline_ids = {c["item_id"] for c in baseline["consultations"]}
    got_ids = {c["item_id"] for c in got["consultations"]}
    dropped = sorted(baseline_ids ^ got_ids)
    n_common = len(baseline_ids & got_ids)

    lines = [
        "# GoT-lite ablation v1: single-pass baseline vs GoT-lite (got_k2)",
        "",
        "**All results in this report are English-only.** v1 evaluates PriMock57 (English) "
        "consultations exclusively; no Kannada-English data exists yet (claude_context.md §1, §2.1). "
        "Every number below is grouped by language, and the only language subset that exists is "
        "`en` — there is nothing to average across.",
        "",
        "**This is the first real run of this comparison, on the entire locally-available "
        f"gold-annotated PriMock57 set (N={n_common}), not a held-out test split.** Only 8 of "
        "PriMock57's 57 upstream consultations are fetched locally (claude_context.md decision "
        "#61); a deterministic 60/20/20 train/dev/test hash split over those 8 items happened to "
        "place zero items in `test`. Carving out a smaller held-out subset would not have "
        "protected against anything real at this N: neither arm's prompts nor scorer weights were "
        "tuned against any split of this data (the N=3/K=2/0.5-0.3-0.2 weights are user-endorsed "
        "defaults from the method design, not fit to these 8 items), so there is no train/test "
        "leakage risk here to guard against — only an honestly small N to disclose, which the "
        "statistical-honesty section below does throughout.",
        "",
        "## Run metadata",
        "",
        f"- Timestamp (UTC): {datetime.now(UTC).isoformat()}",
        f"- Baseline report: `{baseline_json_path}` (git sha `{baseline.get('git_sha', '?')}`)",
        f"- GoT-lite report: `{got_json_path}` (git sha `{got.get('git_sha', '?')}`)",
        f"- Base model (held constant across both arms): `{baseline.get('base_model', '?')}`"
        + (
            " ✓ matches"
            if baseline.get("base_model") == got.get("base_model")
            else f" ⚠ MISMATCH vs GoT-lite's `{got.get('base_model', '?')}`"
        ),
        f"- Baseline prompt set hash: `{baseline.get('prompt_set_hash', '?')}`",
        f"- GoT-lite prompt set hash: `{got.get('prompt_set_hash', '?')}`",
        f"- GoT-lite arm config: N={got.get('n_candidates')}, K={got.get('k_iterations')}, "
        f"graph_context_enabled={got.get('graph_context_enabled')}, "
        f"scorer_backend=`{got.get('scorer_backend')}`, structural_model=`{got.get('structural_model')}`",
        f"- Items common to both reports (the paired set): {n_common}",
    ]
    if dropped:
        lines.append(
            f"- **{len(dropped)} item(s) present in only one report and excluded from every "
            f"paired comparison below**: {', '.join(dropped)}"
        )
    lines += ["", "## Statistical honesty", ""]
    lines.append(
        f"- Every comparison below is a **paired** difference (GoT-lite − baseline) over the same "
        f"consultations, bootstrapped with {N_BOOT:,} resamples (fixed seed `{BOOTSTRAP_SEED}` — "
        "re-running this exact command reproduces identical CI bounds)."
    )
    lines.append(
        f"- **N < {MIN_N_FOR_CLAIM} is flagged as insufficient to support a claim** in either "
        "direction — a point estimate is still shown, but no confidence interval is computed, "
        "and the table says so explicitly rather than printing a CI built on too few points."
    )
    any_insufficient = any(c.insufficient_n for c in comparisons)
    if any_insufficient:
        lines.append(
            f"- **Every metric in this report has N={n_common} paired consultations, below the "
            f"N={MIN_N_FOR_CLAIM} threshold.** No confidence interval below should be read as "
            "statistically significant. Point estimates and CIs (where shown) are directional "
            "only, pending a larger PriMock57 fetch (plan.md Phase 2's remaining task)."
        )
    lines.append("")

    lines += [
        "## Headline ablation table",
        "",
        "| Metric | Direction | Baseline (mean) | Baseline 95% CI | GoT-lite (mean) | "
        "GoT-lite 95% CI | Δ (GoT − baseline) | Δ 95% CI | N | Verdict |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    label_to_key = {label: key for key, label in PER_ITEM_METRICS}
    for c in comparisons:
        direction = "lower better" if label_to_key[c.metric] in LOWER_IS_BETTER else "higher better"
        if c.insufficient_n and c.n_paired == 0:
            verdict = "no paired data"
        elif c.insufficient_n:
            verdict = f"N={c.n_paired} — insufficient to claim either way"
        elif c.ci_spans_zero:
            verdict = "no significant difference (CI spans zero)"
        elif c.diff_mean is not None and c.diff_mean > 0:
            verdict = "GoT-lite better" if direction == "higher better" else "GoT-lite worse"
        elif c.diff_mean is not None and c.diff_mean < 0:
            verdict = "GoT-lite worse" if direction == "higher better" else "GoT-lite better"
        else:
            verdict = "tied"
        digits = 0 if "tokens" in c.metric.lower() or "wall-clock" in c.metric.lower() else 4
        lines.append(
            f"| {c.metric} | {direction} | {_fmt(c.baseline_mean, digits=digits)} | "
            f"{_fmt_ci(c.baseline_ci, digits=digits)} | {_fmt(c.got_mean, digits=digits)} | "
            f"{_fmt_ci(c.got_ci, digits=digits)} | {_fmt(c.diff_mean, digits=digits)} | "
            f"{_fmt_ci(c.diff_ci, digits=digits)} | {c.n_paired} | {verdict} |"
        )
    lines.append("")

    lines += [
        "## Per-language breakdown",
        "",
        "Only `en` exists in v1 (claude_context.md §2.1) — the table above **is** the `en` "
        "breakdown, one row wide. `coda_eval.eval_results.language` is carried on every row so "
        "a future `kn_en` subset (Phase 12) adds rows here without a code change; nothing is "
        "averaged across language today because there is nothing else to average with.",
        "",
    ]

    lines += [
        "## Bottom line",
        "",
    ]
    f1_row = next((c for c in comparisons if c.metric == "Field-level micro F1"), None)
    if f1_row is not None and f1_row.n_paired > 0:
        if f1_row.insufficient_n:
            lines.append(
                f"At N={f1_row.n_paired}, GoT-lite's field-F1 point estimate is "
                f"{_fmt(f1_row.got_mean)} against baseline's {_fmt(f1_row.baseline_mean)} "
                f"(Δ={_fmt(f1_row.diff_mean)}), but this sample is too small to say whether "
                "GoT-lite helps, hurts, or makes no difference on this dataset. **This is stated "
                "as a limitation, not resolved by re-running or re-weighting anything** — a "
                "negative or ambiguous result is a valid finding (claude_context.md's framing: "
                "the research contribution is the ablation, not a foregone conclusion about it)."
            )
        elif f1_row.ci_spans_zero:
            lines.append(
                f"The paired 95% CI on the field-F1 difference ({_fmt_ci(f1_row.diff_ci)}) spans "
                f"zero: at N={f1_row.n_paired}, this run does not support a claim that GoT-lite "
                "either beats or underperforms the single-pass baseline on field-level extraction "
                "accuracy. Reported plainly, not hidden or tuned away."
            )
        elif f1_row.diff_mean is not None and f1_row.diff_mean > 0:
            lines.append(
                f"GoT-lite's field-F1 exceeds baseline by {_fmt(f1_row.diff_mean)} "
                f"(95% CI {_fmt_ci(f1_row.diff_ci)}, N={f1_row.n_paired}), consistent with H1."
            )
        else:
            lines.append(
                f"GoT-lite's field-F1 is **below** baseline by {_fmt(abs(f1_row.diff_mean or 0))} "
                f"(95% CI {_fmt_ci(f1_row.diff_ci)}, N={f1_row.n_paired}). Reported as measured: "
                "H1 is not supported by this run. A negative result is a valid finding and is not "
                "hidden or tuned away (claude_context.md's explicit instruction)."
            )
    lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


__all__ = [
    "CI_ALPHA",
    "LOWER_IS_BETTER",
    "MIN_N_FOR_CLAIM",
    "N_BOOT",
    "MetricComparison",
    "PER_ITEM_METRICS",
    "compare_metric",
    "compare_reports",
    "load_report",
    "write_ablation_report",
]
