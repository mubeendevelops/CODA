"""Loads coda_eval's own versioned prompt files (currently just the
hallucination judge, metrics/hallucination.py) — the same
`prompts/{language}/{version}/` convention as `nlp_service/prompts.py`
(claude_context.md decision #68), a separate directory because `python/eval`
is its own uv workspace member with its own prompts, not a place that
should reach into nlp-service's `prompts/` tree.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import cache
from pathlib import Path

PROMPT_VERSION = "v1"
DEFAULT_LANGUAGE = "en"

_PROMPTS_ROOT = Path(__file__).resolve().parents[2] / "prompts"


@dataclass(frozen=True, slots=True)
class HallucinationJudgePrompts:
    system: str
    user_template: str


def _version_dir(language: str, version: str) -> Path:
    d = _PROMPTS_ROOT / language / version
    if not d.is_dir():
        raise FileNotFoundError(
            f"coda_eval.prompts: no prompt directory at {d} "
            f"(language={language!r}, version={version!r})"
        )
    return d


def _read(language: str, version: str, filename: str) -> str:
    return (_version_dir(language, version) / filename).read_text(encoding="utf-8")


@cache
def load_hallucination_judge_prompts(
    language: str = DEFAULT_LANGUAGE, version: str = PROMPT_VERSION
) -> HallucinationJudgePrompts:
    return HallucinationJudgePrompts(
        system=_read(language, version, "hallucination_judge_system.md"),
        user_template=_read(language, version, "hallucination_judge_user.md"),
    )


@cache
def prompt_set_hash(language: str = DEFAULT_LANGUAGE, version: str = PROMPT_VERSION) -> str:
    d = _version_dir(language, version)
    h = hashlib.sha256()
    for path in sorted(d.iterdir()):
        if path.is_file():
            h.update(path.name.encode("utf-8"))
            h.update(b"\x00")
            h.update(path.read_bytes())
    return h.hexdigest()


__all__ = [
    "DEFAULT_LANGUAGE",
    "PROMPT_VERSION",
    "HallucinationJudgePrompts",
    "load_hallucination_judge_prompts",
    "prompt_set_hash",
]
