"""nlp-service environment configuration, mirroring asr_service/config.py's
shape: `_require`/`_get` helpers, a frozen dataclass, `from_env()`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _require(name: str) -> str:
    val = os.environ.get(name, "")
    if not val:
        raise RuntimeError(f"nlp_service: required environment variable {name} is not set")
    return val


def _get(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True, slots=True)
class NlpConfig:
    groq_api_key: str
    """Required for extraction and summary calls (single-pass mode)."""
    json_repair_max_attempts: int
    """Bounded repair-retry budget for a failed extraction validation
    (Phase 4 requirement: record JSON validity as a metric, never silently
    fix a failure — see extraction.py). 0 means "no repair, fail on first
    invalid response"."""
    llm_timeout_s: float
    summary_min_chars: int
    """Sanity floor for the summary call — not schema-gated (free text), but
    an empty or near-empty summary is still a real failure worth catching."""

    @classmethod
    def from_env(cls) -> NlpConfig:
        return cls(
            groq_api_key=_require("GROQ_API_KEY"),
            json_repair_max_attempts=int(_get("NLP_JSON_REPAIR_MAX_ATTEMPTS", "2")),
            llm_timeout_s=float(_get("NLP_LLM_TIMEOUT_S", "60")),
            summary_min_chars=int(_get("NLP_SUMMARY_MIN_CHARS", "20")),
        )


__all__ = ["NlpConfig"]
