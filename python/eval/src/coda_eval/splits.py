"""Train/dev/test split assignment, enforced by conversation/session ID —
never by utterance (plan.md Phase 2: "the leakage risk in the prior
report"). Splits are computed **per language subset independently**, so a
v2 Kannada-English subset gets its own train/dev/test partition rather than
sharing session-ID hash space with English — the harness is multi-language
from day one even though v1 only ever populates `language == "en"`
(claude_context.md §2.1: never collapse this to a single-language design).
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import replace

from coda_eval.registry import ManifestEntry

DEFAULT_SPLIT_RATIOS: dict[str, float] = {"train": 0.6, "dev": 0.2, "test": 0.2}


class SplitLeakageError(Exception):
    """Raised when a session/conversation ID would land in more than one
    split, or when a split assignment is not partitioned per language."""


def _split_for_session(
    language: str, session_id: str, ratios: dict[str, float], seed: str
) -> str:
    """Deterministic, seed-able hash bucket — the same (language, session_id,
    seed) always yields the same split, without persisting a lookup table.
    """
    digest = hashlib.sha256(f"{seed}:{language}:{session_id}".encode()).hexdigest()
    frac = int(digest[:8], 16) / 0xFFFFFFFF
    cumulative = 0.0
    names = list(ratios.keys())
    for name in names:
        cumulative += ratios[name]
        if frac < cumulative or name == names[-1]:
            return name
    return names[-1]  # pragma: no cover - unreachable, ratios sum to 1.0


def assign_splits(
    entries: list[ManifestEntry],
    *,
    ratios: dict[str, float] | None = None,
    seed: str = "coda-eval-v1",
) -> list[ManifestEntry]:
    """Returns new entries with `.split` set, computed independently per
    `language` value present in `entries` (see module docstring).
    """
    ratios = ratios or DEFAULT_SPLIT_RATIOS
    if abs(sum(ratios.values()) - 1.0) > 1e-6:
        raise ValueError(f"split ratios must sum to 1.0, got {ratios}")

    out = []
    for entry in entries:
        split = _split_for_session(entry.language, entry.session_id, ratios, seed)
        out.append(replace(entry, split=split))
    return out


def assert_no_leakage(entries: list[ManifestEntry]) -> None:
    """Fails loudly (raises `SplitLeakageError`) if any (language,
    session_id) pair maps to more than one split, or any entry has no split
    assigned yet. Call this after `assign_splits` and again immediately
    before any metric is computed from the splits — cheap insurance against
    a future refactor silently reintroducing utterance-level splitting.
    """
    seen: dict[tuple[str, str], set[str]] = defaultdict(set)
    unassigned = []
    for entry in entries:
        if entry.split is None:
            unassigned.append(entry.item_id)
            continue
        seen[(entry.language, entry.session_id)].add(entry.split)

    if unassigned:
        raise SplitLeakageError(
            f"{len(unassigned)} entries have no split assigned: {unassigned[:10]}"
            + (" ..." if len(unassigned) > 10 else "")
        )

    leaked = {key: splits for key, splits in seen.items() if len(splits) > 1}
    if leaked:
        raise SplitLeakageError(
            "session ID(s) span more than one split (utterance-level leakage): "
            f"{dict(list(leaked.items())[:10])}"
        )


__all__ = ["DEFAULT_SPLIT_RATIOS", "SplitLeakageError", "assert_no_leakage", "assign_splits"]
