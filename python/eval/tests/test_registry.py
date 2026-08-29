import tempfile
from pathlib import Path

from coda_eval.registry import (
    DatasetSummary,
    ManifestEntry,
    read_dataset_registry,
    read_manifest,
    write_dataset_registry,
    write_manifest,
)


def test_manifest_round_trip() -> None:
    entries = [
        ManifestEntry(
            item_id="ds:s1",
            dataset="ds",
            session_id="s1",
            language="en",
            modality="audio_grounded",
            provenance="real",
            licence="CC-BY-4.0",
            source_url="https://example.invalid",
            audio_path="/tmp/a.wav",
            extra={"n": 3},
        )
    ]
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "m.jsonl"
        write_manifest(entries, path)
        back = read_manifest(path)
        assert back == entries


def test_dataset_registry_round_trip() -> None:
    summaries = [
        DatasetSummary(
            name="primock57",
            source_url="https://github.com/babylonhealth/primock57",
            licence="CC-BY-4.0",
            modality="audio_grounded",
            languages=["en"],
            intended_use="dev+eval",
            n_items_available_locally=8,
            n_items_upstream=57,
            manifest_path="/tmp/primock57.manifest.jsonl",
        )
    ]
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "registry.yaml"
        write_dataset_registry(summaries, path)
        back = read_dataset_registry(path)
        assert back == summaries
        # licence must be human-readable in the raw file too (Phase 2 AC 1)
        assert "CC-BY-4.0" in path.read_text(encoding="utf-8")
