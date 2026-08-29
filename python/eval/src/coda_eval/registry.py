"""The dataset registry: a per-item manifest format shared by every loader,
plus a per-dataset summary written to `data/registry.yaml` (plan.md Phase 2
acceptance criterion 1 — "registry lists licence per dataset").

The manifest is intentionally a superset of what any single dataset can
populate: PriMock57 is audio-grounded (audio + reference transcript +
reference RTTM), MTS-Dialog/ACI-Bench are text-only (dialogue + note, no
audio) — a text-only entry simply leaves the audio-only fields `None` rather
than the registry having two incompatible entry shapes.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

MODALITY_AUDIO_GROUNDED = "audio_grounded"
MODALITY_TEXT_ONLY = "text_only"

PROVENANCE_REAL = "real"
PROVENANCE_SIMULATED = "simulated"


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """One conversation/session — the unit splits are enforced on
    (never utterance-level; see splits.py).
    """

    item_id: str
    """Globally unique: f"{dataset}:{session_id}"."""
    dataset: str
    session_id: str
    """The conversation/session identifier splits partition by. For
    PriMock57 this is e.g. "day1_consultation01"; for ACI-Bench,
    `encounter_id`; for MTS-Dialog, the row `ID` (each row is already one
    whole dialogue, so there is no narrower session to leak across)."""
    language: str
    """"en" | "kn_en" — only "en" is ever populated in v1 (claude_context.md §2.1)."""
    modality: str
    """MODALITY_AUDIO_GROUNDED | MODALITY_TEXT_ONLY."""
    provenance: str
    """PROVENANCE_REAL | PROVENANCE_SIMULATED — real clinician/patient data
    vs. the project's own simulated role-play corpus (v2 only, currently
    always "real" since v1 has no simulated data)."""
    licence: str
    source_url: str
    split: str | None = None
    """"train" | "dev" | "test" | None (unassigned until splits.py runs)."""
    audio_path: str | None = None
    reference_transcript_path: str | None = None
    """Combined, speaker-labeled reference transcript (plain text)."""
    reference_rttm_path: str | None = None
    """Reference diarization in RTTM format — audio-grounded entries only."""
    reference_note_path: str | None = None
    """Clinician note / target summary, where the dataset provides one."""
    extra: dict[str, Any] = field(default_factory=dict)


def write_manifest(entries: list[ManifestEntry], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(asdict(entry), ensure_ascii=False))
            f.write("\n")


def read_manifest(path: Path) -> list[ManifestEntry]:
    entries: list[ManifestEntry] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entries.append(ManifestEntry(**json.loads(line)))
    return entries


@dataclass(frozen=True, slots=True)
class DatasetSummary:
    """One row of the dataset-level `data/registry.yaml` (Phase 2 AC 1)."""

    name: str
    source_url: str
    licence: str
    modality: str
    languages: list[str]
    intended_use: str
    """"dev" | "eval" | "dev+eval"."""
    n_items_available_locally: int
    n_items_upstream: int
    """Total items the upstream dataset provides, whether or not fetched
    locally — an honest N alongside what's actually on disk."""
    manifest_path: str


def write_dataset_registry(summaries: list[DatasetSummary], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"datasets": [asdict(s) for s in summaries]}
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)


def read_dataset_registry(path: Path) -> list[DatasetSummary]:
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return [DatasetSummary(**row) for row in data.get("datasets", [])]


__all__ = [
    "MODALITY_AUDIO_GROUNDED",
    "MODALITY_TEXT_ONLY",
    "PROVENANCE_REAL",
    "PROVENANCE_SIMULATED",
    "DatasetSummary",
    "ManifestEntry",
    "read_dataset_registry",
    "read_manifest",
    "write_dataset_registry",
    "write_manifest",
]
