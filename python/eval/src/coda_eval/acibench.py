"""ACI-Bench loader: text-only dialogue -> full clinical note pairs
(CC-BY-4.0, github.com/wyim/aci-bench). No audio — for NLP-stage prompt
development, not ASR/DER evaluation.

`encounter_id` is the natural session/conversation unit splits.py partitions
on. The same `encounter_id` can appear in more than one of the upstream
challenge CSVs (e.g. a shared-task train/test split); rows are de-duplicated
by `encounter_id` so this loader never emits the same consultation twice
under two different item IDs, which would otherwise let it silently leak
across our own splits regardless of the upstream file it came from.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from pathlib import Path

from coda_eval.config import DATA_PROCESSED_DIR, DATA_RAW_DIR
from coda_eval.registry import MODALITY_TEXT_ONLY, PROVENANCE_REAL, ManifestEntry

DATASET_NAME = "aci_bench"
SOURCE_URL = "https://github.com/wyim/aci-bench"
LICENCE = "CC-BY-4.0"

_CHALLENGE_DATA_DIR = "data/challenge_data"


def _iter_csvs(raw_dir: Path) -> Iterator[tuple[str, dict[str, str]]]:
    challenge_dir = raw_dir / _CHALLENGE_DATA_DIR
    if not challenge_dir.is_dir():
        return
    for csv_path in sorted(challenge_dir.glob("*.csv")):
        if csv_path.name.endswith("_metadata.csv"):
            continue
        with csv_path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                yield csv_path.name, row


def build_manifest(
    *, raw_dir: Path | None = None, processed_dir: Path | None = None
) -> list[ManifestEntry]:
    raw_dir = raw_dir or (DATA_RAW_DIR / "aci-bench")
    processed_dir = processed_dir or (DATA_PROCESSED_DIR / DATASET_NAME)
    seen_encounters: set[str] = set()
    entries = []
    for source_file, row in _iter_csvs(raw_dir):
        encounter_id = row.get("encounter_id", "").strip()
        dialogue = row.get("dialogue", "")
        note = row.get("note", "")
        if not encounter_id or not dialogue or encounter_id in seen_encounters:
            continue
        seen_encounters.add(encounter_id)

        transcript_path = processed_dir / "dialogues" / f"{encounter_id}.txt"
        note_path = processed_dir / "notes" / f"{encounter_id}.txt"
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.parent.mkdir(parents=True, exist_ok=True)
        transcript_path.write_text(dialogue, encoding="utf-8")
        note_path.write_text(note, encoding="utf-8")

        entries.append(
            ManifestEntry(
                item_id=f"{DATASET_NAME}:{encounter_id}",
                dataset=DATASET_NAME,
                session_id=encounter_id,
                language="en",
                modality=MODALITY_TEXT_ONLY,
                provenance=PROVENANCE_REAL,
                licence=LICENCE,
                source_url=SOURCE_URL,
                reference_transcript_path=str(transcript_path),
                reference_note_path=str(note_path),
                extra={"dataset_tag": row.get("dataset", ""), "source_file": source_file},
            )
        )
    return entries


__all__ = ["DATASET_NAME", "LICENCE", "SOURCE_URL", "build_manifest"]
