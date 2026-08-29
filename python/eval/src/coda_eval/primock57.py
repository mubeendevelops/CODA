"""PriMock57 loader: audio-grounded ASR/DER evaluation set (CC-BY-4.0,
github.com/babylonhealth/primock57). 57 mock primary-care consultations,
each with separate close-talk doctor/patient audio, utterance-level Praat
TextGrid transcripts per speaker, and a clinician note.

Builds, per consultation actually present on disk:
- a mixed single-channel audio file (overlaying the two close-talk tracks —
  matching what a real consultation recording looks like, since our system
  never sees separate per-speaker channels)
- a combined, chronologically-ordered plain-text reference transcript (for
  WER/CER)
- a reference RTTM (for DER) — each speaker's non-empty TextGrid intervals
  *are* that speaker's reference diarization segments, since doctor/patient
  were each recorded on their own dedicated channel

Only consultations with real (non-LFS-pointer) audio on disk are included —
`git lfs pull --include=...` is a manual, deliberately-scoped step (§10's
"~1h clone" is for the *full* 57-consultation corpus; this project fetched a
subset — see docs/eval/*.md for exactly which and why).
"""

from __future__ import annotations

import json
from pathlib import Path

from pydub import AudioSegment  # type: ignore[import-untyped]

from coda_eval.config import DATA_PROCESSED_DIR, DATA_RAW_DIR
from coda_eval.registry import (
    MODALITY_AUDIO_GROUNDED,
    PROVENANCE_REAL,
    ManifestEntry,
)
from coda_eval.rttm import RttmSegment, write_rttm
from coda_eval.textgrid import non_empty_intervals, strip_transcript_tags

DATASET_NAME = "primock57"
SOURCE_URL = "https://github.com/babylonhealth/primock57"
LICENCE = "CC-BY-4.0"
N_ITEMS_UPSTREAM = 57

_LFS_POINTER_MAX_BYTES = 4096
"""An un-pulled LFS pointer file is ~130 bytes of text; real PriMock57 audio
is multiple MB. Anything under this is treated as "not actually fetched"."""


def _is_real_audio(path: Path) -> bool:
    return path.exists() and path.stat().st_size > _LFS_POINTER_MAX_BYTES


def _discover_session_ids(raw_dir: Path) -> list[str]:
    audio_dir = raw_dir / "audio"
    if not audio_dir.is_dir():
        return []
    ids = sorted(
        p.name.removesuffix("_doctor.wav")
        for p in audio_dir.glob("*_doctor.wav")
        if _is_real_audio(p) and _is_real_audio(audio_dir / p.name.replace("_doctor", "_patient"))
    )
    return ids


def _build_reference_transcript(
    doctor_textgrid: Path, patient_textgrid: Path
) -> list[tuple[float, str, str]]:
    """Returns (start_s, speaker, text) tuples, chronologically ordered,
    tags stripped — the same collation `scripts/textgrid_to_transcript.py`
    (PriMock57's own tool) performs, reimplemented here rather than shelled
    out to (no Python 2/3 ambiguity, no extra subprocess dependency).
    """
    rows = []
    for path, speaker in ((doctor_textgrid, "Doctor"), (patient_textgrid, "Patient")):
        for iv in non_empty_intervals(str(path)):
            text = strip_transcript_tags(iv.text).strip()
            if text:
                rows.append((iv.start_s, speaker, text))
    rows.sort(key=lambda r: r[0])
    return rows


def _build_reference_rttm_segments(
    doctor_textgrid: Path, patient_textgrid: Path, uri: str
) -> list[RttmSegment]:
    segments = []
    for path, speaker in ((doctor_textgrid, "Doctor"), (patient_textgrid, "Patient")):
        for iv in non_empty_intervals(str(path)):
            duration = iv.end_s - iv.start_s
            if duration > 0:
                segments.append(
                    RttmSegment(uri=uri, start_s=iv.start_s, duration_s=duration, speaker=speaker)
                )
    segments.sort(key=lambda s: s.start_s)
    return segments


def _mix_audio(doctor_wav: Path, patient_wav: Path, out_path: Path) -> None:
    doctor = AudioSegment.from_file(doctor_wav)
    patient = AudioSegment.from_file(patient_wav)
    mixed = doctor.overlay(patient)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    mixed.export(out_path, format="wav")


def build_manifest(
    *, raw_dir: Path | None = None, processed_dir: Path | None = None
) -> list[ManifestEntry]:
    """Scans `data/raw/primock57` for consultations with real audio present,
    derives the mixed-audio/reference-transcript/reference-RTTM artifacts
    into `data/processed/primock57/`, and returns one `ManifestEntry` per
    consultation found. Idempotent — re-running skips consultations whose
    derived artifacts already exist and are newer than the source TextGrids.
    """
    raw_dir = raw_dir or (DATA_RAW_DIR / DATASET_NAME)
    processed_dir = processed_dir or (DATA_PROCESSED_DIR / DATASET_NAME)
    session_ids = _discover_session_ids(raw_dir)

    entries = []
    for session_id in session_ids:
        doctor_wav = raw_dir / "audio" / f"{session_id}_doctor.wav"
        patient_wav = raw_dir / "audio" / f"{session_id}_patient.wav"
        doctor_tg = raw_dir / "transcripts" / f"{session_id}_doctor.TextGrid"
        patient_tg = raw_dir / "transcripts" / f"{session_id}_patient.TextGrid"
        note_path = raw_dir / "notes" / f"{session_id}.json"

        if not (doctor_tg.exists() and patient_tg.exists()):
            continue

        mixed_audio_path = processed_dir / "audio" / f"{session_id}.wav"
        transcript_path = processed_dir / "transcripts" / f"{session_id}.ref.txt"
        rttm_path = processed_dir / "rttm" / f"{session_id}.ref.rttm"

        if not mixed_audio_path.exists():
            _mix_audio(doctor_wav, patient_wav, mixed_audio_path)

        rows = _build_reference_transcript(doctor_tg, patient_tg)
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        transcript_path.write_text(
            "\n".join(f"{speaker}: {text}" for _start, speaker, text in rows) + "\n",
            encoding="utf-8",
        )

        rttm_segments = _build_reference_rttm_segments(doctor_tg, patient_tg, uri=session_id)
        rttm_path.parent.mkdir(parents=True, exist_ok=True)
        write_rttm(rttm_segments, str(rttm_path))

        entries.append(
            ManifestEntry(
                item_id=f"{DATASET_NAME}:{session_id}",
                dataset=DATASET_NAME,
                session_id=session_id,
                language="en",
                modality=MODALITY_AUDIO_GROUNDED,
                provenance=PROVENANCE_REAL,
                licence=LICENCE,
                source_url=SOURCE_URL,
                audio_path=str(mixed_audio_path),
                reference_transcript_path=str(transcript_path),
                reference_rttm_path=str(rttm_path),
                reference_note_path=str(note_path) if note_path.exists() else None,
                extra={"n_utterances": len(rows)},
            )
        )
    return entries


def load_reference_note(entry: ManifestEntry) -> dict[str, object] | None:
    if not entry.reference_note_path:
        return None
    with open(entry.reference_note_path, encoding="utf-8") as f:
        result: dict[str, object] = json.load(f)
        return result


__all__ = [
    "DATASET_NAME",
    "LICENCE",
    "N_ITEMS_UPSTREAM",
    "SOURCE_URL",
    "build_manifest",
    "load_reference_note",
]
