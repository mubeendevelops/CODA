"""RTTM (Rich Transcription Time Marked) read/write — the standard format
for diarization reference/hypothesis, and what pyannote.metrics consumes via
`pyannote.core.Annotation`.

One line per speech segment:
    SPEAKER <uri> 1 <start_s> <duration_s> <NA> <NA> <speaker_label> <NA> <NA>
"""

from __future__ import annotations

from dataclasses import dataclass

from pyannote.core import Annotation, Segment  # type: ignore[import-untyped]


@dataclass(frozen=True, slots=True)
class RttmSegment:
    uri: str
    start_s: float
    duration_s: float
    speaker: str


def write_rttm(segments: list[RttmSegment], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for seg in segments:
            f.write(
                f"SPEAKER {seg.uri} 1 {seg.start_s:.3f} {seg.duration_s:.3f} "
                f"<NA> <NA> {seg.speaker} <NA> <NA>\n"
            )


def read_rttm(path: str) -> list[RttmSegment]:
    segments = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = line.split()
            if not parts or parts[0] != "SPEAKER":
                continue
            segments.append(
                RttmSegment(
                    uri=parts[1],
                    start_s=float(parts[3]),
                    duration_s=float(parts[4]),
                    speaker=parts[7],
                )
            )
    return segments


def rttm_to_annotation(segments: list[RttmSegment], uri: str) -> Annotation:
    annotation = Annotation(uri=uri)
    for seg in segments:
        if seg.uri != uri:
            continue
        annotation[Segment(seg.start_s, seg.start_s + seg.duration_s)] = seg.speaker
    return annotation


__all__ = ["RttmSegment", "read_rttm", "rttm_to_annotation", "write_rttm"]
