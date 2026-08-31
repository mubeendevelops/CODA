import json
from pathlib import Path

from click.testing import CliRunner

from coda_eval import cli
from coda_eval.gold_schema import validate_gold_dict


def _write_reference_transcript(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "Doctor: hello, what brings you in today\n"
        "Patient: I've had a sore throat for three days\n"
        "Doctor: any fever\n"
        "Patient: no fever\n",
        encoding="utf-8",
    )


def _write_manifest(manifest_path: Path, *, session_id: str, transcript_path: Path) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "item_id": f"primock57:{session_id}",
        "dataset": "primock57",
        "session_id": session_id,
        "language": "en",
        "modality": "audio_grounded",
        "provenance": "real",
        "licence": "CC-BY-4.0",
        "source_url": "https://example.test",
        "split": None,
        "audio_path": None,
        "reference_transcript_path": str(transcript_path),
        "reference_rttm_path": None,
        "reference_note_path": None,
        "extra": {},
    }
    manifest_path.write_text(json.dumps(entry) + "\n", encoding="utf-8")


def test_gold_scaffold_then_validate_roundtrip(tmp_path, monkeypatch) -> None:
    registry_dir = tmp_path / "registry"
    gold_dir = tmp_path / "gold"
    transcript_path = tmp_path / "raw" / "day1_consultation01.ref.txt"
    _write_reference_transcript(transcript_path)
    _write_manifest(
        registry_dir / "primock57.manifest.jsonl",
        session_id="day1_consultation01",
        transcript_path=transcript_path,
    )

    monkeypatch.setattr(cli, "DATA_REGISTRY_DIR", registry_dir)
    monkeypatch.setattr(cli, "DATA_GOLD_DIR", gold_dir)

    runner = CliRunner()
    result = runner.invoke(cli.main, ["gold-scaffold", "--item", "day1_consultation01"])
    assert result.exit_code == 0, result.output

    out_path = gold_dir / "primock57" / "day1_consultation01.gold.json"
    assert out_path.exists()
    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert data["n_turns"] == 4
    assert data["speakers"][0]["speaker"] == "Doctor"

    # scaffold is empty-but-shape-valid -> semantic validation still fails (empty summary)
    validation = validate_gold_dict(data)
    assert not validation.valid
    assert any("summary" in e for e in validation.errors)

    result = runner.invoke(cli.main, ["gold-validate", "--all"])
    assert result.exit_code == 1

    # fill it in minimally and re-validate via the CLI
    data["summary"] = "Patient reports a three-day sore throat, no fever."
    data["fields"]["chief_complaint"] = {
        "value": "sore throat for three days",
        "source_turn_ids": [1],
    }
    out_path.write_text(json.dumps(data), encoding="utf-8")

    result = runner.invoke(cli.main, ["gold-validate", str(out_path)])
    assert result.exit_code == 0, result.output


def test_gold_scaffold_skips_existing_without_force(tmp_path, monkeypatch) -> None:
    registry_dir = tmp_path / "registry"
    gold_dir = tmp_path / "gold"
    transcript_path = tmp_path / "raw" / "day1_consultation01.ref.txt"
    _write_reference_transcript(transcript_path)
    _write_manifest(
        registry_dir / "primock57.manifest.jsonl",
        session_id="day1_consultation01",
        transcript_path=transcript_path,
    )

    monkeypatch.setattr(cli, "DATA_REGISTRY_DIR", registry_dir)
    monkeypatch.setattr(cli, "DATA_GOLD_DIR", gold_dir)

    runner = CliRunner()
    runner.invoke(cli.main, ["gold-scaffold", "--item", "day1_consultation01"])
    out_path = gold_dir / "primock57" / "day1_consultation01.gold.json"
    out_path.write_text('{"marker": true}', encoding="utf-8")

    runner.invoke(cli.main, ["gold-scaffold", "--item", "day1_consultation01"])
    assert json.loads(out_path.read_text(encoding="utf-8")) == {"marker": True}

    result = runner.invoke(cli.main, ["gold-scaffold", "--item", "day1_consultation01", "--force"])
    assert result.exit_code == 0
    assert json.loads(out_path.read_text(encoding="utf-8")) != {"marker": True}


def test_gold_scaffold_rejects_non_primock57_dataset() -> None:
    runner = CliRunner()
    result = runner.invoke(cli.main, ["gold-scaffold", "--dataset", "mts_dialog", "--item", "x"])
    assert result.exit_code == 1
