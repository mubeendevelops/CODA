"""Gold annotation file I/O and scaffolding. A gold file is hand-edited by a
human annotator after `coda-eval gold-scaffold` generates the shell — see
docs/eval/annotation_guide.md for the annotation instructions and
gold_schema.py for the format itself.

Turn numbering convention (documented in the annotation guide too): `turn_id`
is the 0-indexed line number in the dataset's reference transcript file (one
"Speaker: text" line per turn, chronologically ordered) — the same numbering
nlp_service's extraction prompt assigns as `turn_index` when the eval runner
feeds it the identical transcript text, so a gold `source_turn_ids` and a
hypothesis `source_turn_ids` are directly comparable without translation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from coda_eval.gold_schema import GOLD_SCHEMA_VERSION

_EMPTY_FIELDS: dict[str, object] = {
    "chief_complaint": None,
    "hopi": None,
    "examination_findings": None,
    "treatment_plan": None,
    "past_medical_history": [],
    "medications": [],
    "allergies": [],
    "provisional_diagnosis": [],
    "investigations_advised": [],
}


@dataclass(frozen=True, slots=True)
class ReferenceTurn:
    turn_id: int
    speaker: str
    text: str


def parse_reference_transcript(path: Path) -> list[ReferenceTurn]:
    """Parses the "Speaker: text" per-line reference transcript format every
    coda_eval loader (primock57.py etc.) already writes, into turn-indexed
    rows. This is the single source of truth for both the gold file's
    `speakers` array and the transcript text handed to extraction, so the
    two are always aligned by construction.
    """
    turns = []
    with path.open(encoding="utf-8") as f:
        for idx, line in enumerate(f):
            line = line.rstrip("\n")
            if not line:
                continue
            speaker, _, text = line.partition(": ")
            turns.append(ReferenceTurn(turn_id=idx, speaker=speaker.strip(), text=text.strip()))
    return turns


def transcript_turns_text(turns: list[ReferenceTurn]) -> str:
    """The `{transcript_turns}` block nlp_service's extraction_user.md
    template expects: `turn_index: speaker: text` per line."""
    return "\n".join(f"{t.turn_id}: {t.speaker}: {t.text}" for t in turns)


def scaffold_gold_dict(
    *,
    item_id: str,
    dataset: str,
    session_id: str,
    language: str,
    annotator: str,
    turns: list[ReferenceTurn],
) -> dict[str, object]:
    """An empty-but-valid-shape gold record: real speaker labels (already
    known for PriMock57 from its per-channel TextGrids, so this step is
    free, not re-annotated by hand), fields left null/empty for a human to
    fill in.
    """
    return {
        "schema_version": GOLD_SCHEMA_VERSION,
        "item_id": item_id,
        "dataset": dataset,
        "session_id": session_id,
        "language": language,
        "annotator": annotator,
        "annotated_at": datetime.now(UTC).isoformat(),
        "n_turns": len(turns),
        "speakers": [
            {
                "turn_id": t.turn_id,
                "speaker": t.speaker if t.speaker in ("Doctor", "Patient") else "Unknown",
            }
            for t in turns
        ],
        "fields": dict(_EMPTY_FIELDS),
        "summary": "",
    }


def write_gold(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_gold(path: Path) -> dict[str, object]:
    result: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    return result


__all__ = [
    "ReferenceTurn",
    "load_gold",
    "parse_reference_transcript",
    "scaffold_gold_dict",
    "transcript_turns_text",
    "write_gold",
]
