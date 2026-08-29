"""MTS-Dialog loader: text-only dialogue -> clinical-note-section pairs
(CC-BY-4.0, github.com/abachaa/MTS-Dialog). No audio — for NLP-stage prompt
development (thought construction, distillation), not ASR/DER evaluation.

Each CSV row is already one complete short dialogue (not a multi-turn
consultation split across rows), so the row's own `ID` is the session unit
splits.py partitions on — there is no narrower granularity to leak across.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from pathlib import Path

from coda_eval.config import DATA_PROCESSED_DIR, DATA_RAW_DIR
from coda_eval.registry import MODALITY_TEXT_ONLY, PROVENANCE_REAL, ManifestEntry

DATASET_NAME = "mts_dialog"
SOURCE_URL = "https://github.com/abachaa/MTS-Dialog"
LICENCE = "CC-BY-4.0"

_CSV_FILES = (
    "Main-Dataset/MTS-Dialog-TrainingSet.csv",
    "Main-Dataset/MTS-Dialog-ValidationSet.csv",
    "Main-Dataset/MTS-Dialog-TestSet-1-MEDIQA-Chat-2023.csv",
    "Main-Dataset/MTS-Dialog-TestSet-2-MEDIQA-Sum-2023.csv",
)


def _iter_rows(raw_dir: Path) -> Iterator[tuple[str, dict[str, str]]]:
    for rel in _CSV_FILES:
        csv_path = raw_dir / rel
        if not csv_path.exists():
            continue
        with csv_path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                yield rel, row


def build_manifest(
    *, raw_dir: Path | None = None, processed_dir: Path | None = None
) -> list[ManifestEntry]:
    raw_dir = raw_dir or (DATA_RAW_DIR / "MTS-Dialog")
    processed_dir = processed_dir or (DATA_PROCESSED_DIR / DATASET_NAME)
    entries = []
    for source_file, row in _iter_rows(raw_dir):
        row_id = row.get("ID", "").strip()
        dialogue = row.get("dialogue", "")
        section_text = row.get("section_text", "")
        if not row_id or not dialogue:
            continue
        session_id = f"{Path(source_file).stem}:{row_id}"

        transcript_path = processed_dir / "dialogues" / f"{session_id.replace(':', '_')}.txt"
        note_path = processed_dir / "notes" / f"{session_id.replace(':', '_')}.txt"
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.parent.mkdir(parents=True, exist_ok=True)
        transcript_path.write_text(dialogue, encoding="utf-8")
        note_path.write_text(section_text, encoding="utf-8")

        entries.append(
            ManifestEntry(
                item_id=f"{DATASET_NAME}:{session_id}",
                dataset=DATASET_NAME,
                session_id=session_id,
                language="en",
                modality=MODALITY_TEXT_ONLY,
                provenance=PROVENANCE_REAL,
                licence=LICENCE,
                source_url=SOURCE_URL,
                reference_transcript_path=str(transcript_path),
                reference_note_path=str(note_path),
                extra={"section_header": row.get("section_header", ""), "source_file": source_file},
            )
        )
    return entries


__all__ = ["DATASET_NAME", "LICENCE", "SOURCE_URL", "build_manifest"]
