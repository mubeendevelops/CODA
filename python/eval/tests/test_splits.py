import pytest

from coda_eval.registry import ManifestEntry
from coda_eval.splits import SplitLeakageError, assert_no_leakage, assign_splits


def _entry(session_id: str, language: str = "en") -> ManifestEntry:
    return ManifestEntry(
        item_id=f"ds:{session_id}",
        dataset="ds",
        session_id=session_id,
        language=language,
        modality="text_only",
        provenance="real",
        licence="CC-BY-4.0",
        source_url="https://example.invalid",
    )


def test_assign_splits_is_deterministic() -> None:
    entries = [_entry(f"s{i}") for i in range(50)]
    a = assign_splits(entries)
    b = assign_splits(entries)
    assert [e.split for e in a] == [e.split for e in b]


def test_assign_splits_only_uses_known_names() -> None:
    entries = [_entry(f"s{i}") for i in range(50)]
    out = assign_splits(entries)
    assert {e.split for e in out} <= {"train", "dev", "test"}


def test_assign_splits_rejects_bad_ratios() -> None:
    with pytest.raises(ValueError):
        assign_splits([_entry("s1")], ratios={"train": 0.5, "dev": 0.3})


def test_no_leakage_passes_on_clean_splits() -> None:
    entries = assign_splits([_entry(f"s{i}") for i in range(30)])
    assert_no_leakage(entries)  # must not raise


def test_leakage_detected_when_same_session_id_in_two_splits() -> None:
    from dataclasses import replace

    entries = [replace(_entry("s1"), split="train"), replace(_entry("s1"), split="dev")]
    with pytest.raises(SplitLeakageError):
        assert_no_leakage(entries)


def test_leakage_detected_when_split_unassigned() -> None:
    with pytest.raises(SplitLeakageError):
        assert_no_leakage([_entry("s1")])  # split is None


def test_languages_split_independently() -> None:
    """The same session_id string reused across two languages must not be
    treated as the same session — each language gets its own partition.
    """
    entries = [_entry("shared-id", language="en"), _entry("shared-id", language="kn_en")]
    out = assign_splits(entries)
    assert_no_leakage(out)  # two distinct (language, session_id) keys, no collision
