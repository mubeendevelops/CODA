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


@dataclass(frozen=True, slots=True)
class ThoughtConstructionPrompts:
    """GoT-HCS Module 1 (claude_context.md §6, plan.md Phase 6). Separate
    from ExtractionPrompts because the two do genuinely different jobs on
    the same input: extraction writes the 8-field note in one pass (the
    baseline arm), construction decomposes the dialogue into atomic
    citable thoughts (the GoT arm's node-construction step).
    """

    system: str
    user_template: str
    repair_addendum_template: str


@dataclass(frozen=True, slots=True)
class GenerationPrompts:
    """GoT-HCS Module 5's candidate generation. `user_template` takes a
    `{variant_instruction}` so the N candidates differ by prompt as well as
    by subgraph context (decision #98) — the variants themselves live in
    `nlp_service.reasoning.generation`, not here, because they are a policy
    over one prompt rather than N separate prompt files to keep in sync."""

    system: str
    user_template: str
    repair_addendum_template: str


@dataclass(frozen=True, slots=True)
class JudgePrompts:
    """The written rubric for the `llm_judge` scorer backend."""

    system: str
    user_template: str


@dataclass(frozen=True, slots=True)
class RefinePrompts:
    """Module 5's K-iteration critique-and-regenerate pass."""

    system: str
    user_template: str
    repair_addendum_template: str


@dataclass(frozen=True, slots=True)
class GotSummaryPrompts:
    """Module 6's summary, written from the distilled note rather than from
    the transcript — distinct from `SummaryPrompts`, which the baseline arm
    uses over raw turns."""

    system: str
    user_template: str


@dataclass(frozen=True, slots=True)
class EdgePredictionPrompts:
    """GoT-HCS Module 2's LLM half. The rule half (temporal edges from turn
    order) costs no tokens and never reaches a prompt — see edges.py.
    """

    system: str
    user_template: str
    repair_addendum_template: str


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
def load_thought_construction_prompts(
    language: str = DEFAULT_LANGUAGE, version: str = PROMPT_VERSION
) -> ThoughtConstructionPrompts:
    return ThoughtConstructionPrompts(
        system=_read(language, version, "thought_construction_system.md"),
        user_template=_read(language, version, "thought_construction_user.md"),
        repair_addendum_template=_read(language, version, "thought_repair_addendum.md"),
    )


@cache
def load_edge_prediction_prompts(
    language: str = DEFAULT_LANGUAGE, version: str = PROMPT_VERSION
) -> EdgePredictionPrompts:
    return EdgePredictionPrompts(
        system=_read(language, version, "edge_prediction_system.md"),
        user_template=_read(language, version, "edge_prediction_user.md"),
        repair_addendum_template=_read(language, version, "edge_repair_addendum.md"),
    )


@cache
def load_generation_prompts(
    language: str = DEFAULT_LANGUAGE, version: str = PROMPT_VERSION
) -> GenerationPrompts:
    return GenerationPrompts(
        system=_read(language, version, "got_generation_system.md"),
        user_template=_read(language, version, "got_generation_user.md"),
        repair_addendum_template=_read(language, version, "got_repair_addendum.md"),
    )


@cache
def load_judge_prompts(
    language: str = DEFAULT_LANGUAGE, version: str = PROMPT_VERSION
) -> JudgePrompts:
    return JudgePrompts(
        system=_read(language, version, "got_judge_system.md"),
        user_template=_read(language, version, "got_judge_user.md"),
    )


@cache
def load_refine_prompts(
    language: str = DEFAULT_LANGUAGE, version: str = PROMPT_VERSION
) -> RefinePrompts:
    return RefinePrompts(
        system=_read(language, version, "got_refine_system.md"),
        user_template=_read(language, version, "got_refine_user.md"),
        repair_addendum_template=_read(language, version, "got_repair_addendum.md"),
    )


@cache
def load_got_summary_prompts(
    language: str = DEFAULT_LANGUAGE, version: str = PROMPT_VERSION
) -> GotSummaryPrompts:
    return GotSummaryPrompts(
        system=_read(language, version, "got_summary_system.md"),
        user_template=_read(language, version, "got_summary_user.md"),
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
    "ThoughtConstructionPrompts",
    "EdgePredictionPrompts",
    "GenerationPrompts",
    "JudgePrompts",
    "RefinePrompts",
    "GotSummaryPrompts",
    "load_extraction_prompts",
    "load_summary_prompts",
    "load_thought_construction_prompts",
    "load_edge_prediction_prompts",
    "load_generation_prompts",
    "load_judge_prompts",
    "load_refine_prompts",
    "load_got_summary_prompts",
    "prompt_set_hash",
]
