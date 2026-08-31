"""Loads nlp-service's versioned prompt files. Prompt text lives entirely
under `prompts/{language}/{version}/` (claude_context.md §2.1's language-
directory rule, applied from v1 so v2 adds `prompts/kn_en/...` rather than
restructuring) — nothing here or in extraction.py/summary.py should ever
hold an inline prompt string literal.

`prompt_set_hash()` is sha256 over every file in one version directory,
sorted by filename for determinism. This is what SHOULD populate
`RunConfig.prompt_set_hash` (architecture.md §6.1) — today go-api interns
the RunConfig before any prompt file existed and hardcodes that field to
""  (go/internal/http/jobs_handlers.go, "no prompt files exist yet"). That
gap is not closed by this module: nlp-service computes and records its own
prompt_set_hash independently (see db.py), so the ACTUAL prompt version used
is always recorded even when the persisted run_config's own field is stale
or empty — see claude_context.md decision #66 for the follow-up this leaves.
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
class ExtractionPrompts:
    system: str
    user_template: str
    repair_addendum_template: str


@dataclass(frozen=True, slots=True)
class SummaryPrompts:
    system: str
    user_template: str


def _version_dir(language: str, version: str) -> Path:
    d = _PROMPTS_ROOT / language / version
    if not d.is_dir():
        raise FileNotFoundError(
            f"nlp_service.prompts: no prompt directory at {d} "
            f"(language={language!r}, version={version!r})"
        )
    return d


def _read(language: str, version: str, filename: str) -> str:
    path = _version_dir(language, version) / filename
    return path.read_text(encoding="utf-8")


@cache
def load_extraction_prompts(
    language: str = DEFAULT_LANGUAGE, version: str = PROMPT_VERSION
) -> ExtractionPrompts:
    return ExtractionPrompts(
        system=_read(language, version, "extraction_system.md"),
        user_template=_read(language, version, "extraction_user.md"),
        repair_addendum_template=_read(language, version, "repair_addendum.md"),
    )


@cache
def load_summary_prompts(
    language: str = DEFAULT_LANGUAGE, version: str = PROMPT_VERSION
) -> SummaryPrompts:
    return SummaryPrompts(
        system=_read(language, version, "note_summary_prompt_system.md"),
        user_template=_read(language, version, "note_summary_prompt_user.md"),
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
    "PROMPT_VERSION",
    "DEFAULT_LANGUAGE",
    "ExtractionPrompts",
    "SummaryPrompts",
    "load_extraction_prompts",
    "load_summary_prompts",
    "prompt_set_hash",
]
