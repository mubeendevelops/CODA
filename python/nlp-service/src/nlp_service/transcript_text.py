"""Renders a Transcript proto's turns into the plain-text block the
extraction and summary prompts embed via `{transcript_turns}`. Always uses
`text_redacted`, never `text` — this is the one place that matters (ADR-0014:
redacted text is what may reach a third-party LLM).
"""

from __future__ import annotations

from coda.v1 import transcript_pb2

_SPEAKER_LABELS = {
    transcript_pb2.SpeakerRole.SPEAKER_ROLE_DOCTOR: "doctor",
    transcript_pb2.SpeakerRole.SPEAKER_ROLE_PATIENT: "patient",
    transcript_pb2.SpeakerRole.SPEAKER_ROLE_UNKNOWN: "unknown",
}


def render_turns(transcript: transcript_pb2.Transcript) -> str:
    lines = []
    for turn in transcript.turns:
        speaker = _SPEAKER_LABELS.get(turn.speaker_label, "unknown")
        text = turn.text_redacted or turn.text
        lines.append(f"{turn.turn_index}: {speaker}: {text}")
    return "\n".join(lines)


def known_turn_ids(transcript: transcript_pb2.Transcript) -> set[int]:
    return {turn.turn_index for turn in transcript.turns}


__all__ = ["render_turns", "known_turn_ids"]
