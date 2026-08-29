"""Minimal Praat TextGrid (long text format) parser — just enough to read
PriMock57's utterance-level IntervalTier transcripts. Not a general TextGrid
library: PriMock57's files are single-tier, long-format, and that's the only
shape this needs to handle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_INTERVAL_RE = re.compile(
    r'xmin\s*=\s*([\d.eE+-]+)\s*\n\s*xmax\s*=\s*([\d.eE+-]+)\s*\n\s*text\s*=\s*"((?:[^"]|"")*)"',
    re.MULTILINE,
)


@dataclass(frozen=True, slots=True)
class Interval:
    start_s: float
    end_s: float
    text: str


def parse_intervals(textgrid_path: str) -> list[Interval]:
    """Returns every interval, including empty (silence) ones — callers
    filter those out themselves so the choice of what counts as "silence"
    isn't hidden in the parser.
    """
    with open(textgrid_path, encoding="utf-8", errors="replace") as f:
        content = f.read()
    intervals = []
    for m in _INTERVAL_RE.finditer(content):
        start_s = float(m.group(1))
        end_s = float(m.group(2))
        text = m.group(3).replace('""', '"')
        intervals.append(Interval(start_s=start_s, end_s=end_s, text=text))
    return intervals


def non_empty_intervals(textgrid_path: str) -> list[Interval]:
    return [iv for iv in parse_intervals(textgrid_path) if iv.text.strip()]


_TAG_RE = re.compile(r"<UNSURE>|</UNSURE>|<UNIN/?>")


def strip_transcript_tags(text: str) -> str:
    """Removes PriMock57's transcriber annotation tags (`<UNSURE>...</UNSURE>`,
    `<UNIN/>`), keeping the enclosed text for `<UNSURE>` and dropping the
    whole tag for `<UNIN/>` (it marks unintelligible audio — nothing to keep).
    """
    return _TAG_RE.sub("", text)


__all__ = ["Interval", "non_empty_intervals", "parse_intervals", "strip_transcript_tags"]
