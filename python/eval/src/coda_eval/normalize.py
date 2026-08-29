"""Text normalisation for WER/CER — one documented pipeline, applied
identically to hypothesis and reference so neither side is silently favoured.

Steps, in order:
1. Strip PriMock57 transcriber tags (`<UNSURE>...</UNSURE>` keeps the
   enclosed words; `<UNIN/>` — unintelligible audio — is dropped entirely).
2. Lowercase.
3. Remove all punctuation *except* a word-internal apostrophe (so "don't"
   stays one token, not "don" + "t") — a bare trailing/leading apostrophe is
   still stripped.
4. Collapse runs of whitespace to a single space; strip leading/trailing.

Deliberately NOT done: contraction expansion ("don't" -> "do not") or number
normalisation ("3" -> "three"). Both are legitimate WER-normalisation
choices some ASR eval pipelines make, but each is itself a source of
transcription-style bias (which expansion is "correct"?) — leaving both
forms as-is is the more honest default when comparing a system's own output
against a fixed reference corpus, and it's what jiwer's own default
transform omits too.
"""

from __future__ import annotations

import re

from coda_eval.textgrid import strip_transcript_tags

_PUNCT_RE = re.compile(r"[^\w\s']")
_EDGE_APOSTROPHE_RE = re.compile(r"(^'|'$|\s'|'\s)")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    text = strip_transcript_tags(text)
    text = text.lower()
    text = _PUNCT_RE.sub(" ", text)
    text = _EDGE_APOSTROPHE_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text


__all__ = ["normalize_text"]
