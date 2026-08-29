"""Markdown + CSV report writer — `docs/eval/` (plan.md Phase 2/5).

Results are always grouped by (dataset, language) and never averaged across
language subsets, even when v1 only ever populates one (`en`) — the table
still carries the subset column so a `kn_en` row is a data addition later,
not a report-format change (claude_context.md §2.1).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SubsetResult:
    dataset: str
    language: str
    n_items: int
    wer: float | None
    cer: float | None
    der: float | None
    der_missed_detection: float | None
    der_false_alarm: float | None
    der_confusion: float | None
    notes: str = ""


@dataclass(frozen=True, slots=True)
class RunMetadata:
    run_name: str
    git_sha: str
    asr_backend: str
    asr_model: str
    diarization_model: str | None
    timestamp: datetime
    honest_caveats: list[str]


def write_csv(results: list[SubsetResult], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "dataset",
                "language",
                "n_items",
                "wer",
                "cer",
                "der",
                "der_missed_detection",
                "der_false_alarm",
                "der_confusion",
                "notes",
            ]
        )
        for r in results:
            writer.writerow(
                [
                    r.dataset,
                    r.language,
                    r.n_items,
                    _fmt(r.wer),
                    _fmt(r.cer),
                    _fmt(r.der),
                    _fmt(r.der_missed_detection),
                    _fmt(r.der_false_alarm),
                    _fmt(r.der_confusion),
                    r.notes,
                ]
            )


def _fmt(value: float | None) -> str:
    return "" if value is None else f"{value:.4f}"


def write_markdown(results: list[SubsetResult], meta: RunMetadata, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# {meta.run_name}",
        "",
        "**English-only results.** No multilingual (Kannada-English) evaluation has been "
        "performed — v1 is English-only by design (claude_context.md §2.1), and the table "
        "below carries a `language` column so a future `kn_en` row is a data addition, not a "
        "format change.",
        "",
        "These are numbers this project measured itself, on this run, against the public "
        "reference data recorded in `data/registry.yaml` — not copied from any prior report or "
        "the dataset's own paper.",
        "",
        "## Run metadata",
        "",
        f"- Timestamp (UTC): {meta.timestamp.isoformat()}",
        f"- Git SHA: `{meta.git_sha}`",
        f"- ASR backend / model: `{meta.asr_backend}` / `{meta.asr_model}`",
        f"- Diarization model: `{meta.diarization_model or 'not run this session — see caveats'}`",
        "",
    ]
    if meta.honest_caveats:
        lines.append("## Caveats")
        lines.append("")
        for c in meta.honest_caveats:
            lines.append(f"- {c}")
        lines.append("")

    lines.append("## Results by dataset and language subset")
    lines.append("")
    lines.append(
        "| Dataset | Language | N | WER | CER | DER | DER: miss | DER: false alarm | "
        "DER: confusion | Notes |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in results:
        lines.append(
            f"| {r.dataset} | {r.language} | {r.n_items} | {_fmt(r.wer)} | {_fmt(r.cer)} | "
            f"{_fmt(r.der)} | {_fmt(r.der_missed_detection)} | {_fmt(r.der_false_alarm)} | "
            f"{_fmt(r.der_confusion)} | {r.notes} |"
        )
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


def now_utc() -> datetime:
    return datetime.now(UTC)


__all__ = ["RunMetadata", "SubsetResult", "now_utc", "write_csv", "write_markdown"]
