"""WhisperX-style alignment: assign each Whisper word to the diarization
segment it overlaps most, then group consecutive same-speaker words into
turns.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from asr_service.diarize import DiarizedSegment
from asr_service.transcribe import WordResult

UNKNOWN_SPEAKER = "SPEAKER_UNKNOWN"


@dataclass(frozen=True, slots=True)
class WordWithSpeaker:
    word: WordResult
    speaker: str


@dataclass(slots=True)
class TurnDraft:
    speaker: str
    words: list[WordResult] = field(default_factory=list)

    @property
    def start_s(self) -> float:
        return self.words[0].start_s

    @property
    def end_s(self) -> float:
        return self.words[-1].end_s

    @property
    def text(self) -> str:
        return "".join(w.text for w in self.words).strip()

    @property
    def confidence(self) -> float:
        if not self.words:
            return 0.0
        return sum(w.probability for w in self.words) / len(self.words)


def _best_overlap_speaker(word: WordResult, segments: list[DiarizedSegment]) -> str:
    best_speaker = None
    best_overlap = 0.0
    for seg in segments:
        overlap = min(word.end_s, seg.end_s) - max(word.start_s, seg.start_s)
        if overlap > best_overlap:
            best_overlap = overlap
            best_speaker = seg.speaker
    if best_speaker is not None:
        return best_speaker
    if not segments:
        return UNKNOWN_SPEAKER
    # No diarization segment overlaps this word at all (a diarization gap,
    # or a word right at the boundary of VAD-trimmed silence) — fall back to
    # whichever segment's edge is closest to the word's midpoint.
    mid = (word.start_s + word.end_s) / 2
    nearest = min(segments, key=lambda s: min(abs(mid - s.start_s), abs(mid - s.end_s)))
    return nearest.speaker


def assign_speakers(
    words: list[WordResult], segments: list[DiarizedSegment]
) -> list[WordWithSpeaker]:
    return [WordWithSpeaker(word=w, speaker=_best_overlap_speaker(w, segments)) for w in words]


def build_turns(
    words_with_speaker: list[WordWithSpeaker], *, pause_break_s: float
) -> list[TurnDraft]:
    turns: list[TurnDraft] = []
    current: TurnDraft | None = None
    for ws in words_with_speaker:
        starts_new = (
            current is None
            or ws.speaker != current.speaker
            or ws.word.start_s - current.words[-1].end_s > pause_break_s
        )
        if starts_new:
            if current is not None and current.words:
                turns.append(current)
            current = TurnDraft(speaker=ws.speaker)
        assert current is not None
        current.words.append(ws.word)
    if current is not None and current.words:
        turns.append(current)
    return turns


__all__ = ["TurnDraft", "UNKNOWN_SPEAKER", "WordWithSpeaker", "assign_speakers", "build_turns"]
