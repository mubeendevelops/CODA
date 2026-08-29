import pytest

from coda_worker_sdk.storage import build_artifact_key, parse_artifact_key


def test_artifact_key_round_trips() -> None:
    key = build_artifact_key("dev", "consult-1", "asr", "runcfg-1", "transcript", "json")
    assert key == "dev/consultations/consult-1/stages/asr/runcfg-1/transcript.json"

    parsed = parse_artifact_key(key)
    assert parsed.env == "dev"
    assert parsed.consultation_id == "consult-1"
    assert parsed.stage == "asr"
    assert parsed.run_config_id == "runcfg-1"
    assert parsed.kind == "transcript"
    assert parsed.ext == "json"
    assert str(parsed) == key


def test_build_artifact_key_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="closed ArtifactKind"):
        build_artifact_key("dev", "c1", "asr", "r1", "not_a_real_kind", "json")


def test_parse_artifact_key_rejects_malformed_layout() -> None:
    with pytest.raises(ValueError, match="not a stage artifact key"):
        parse_artifact_key("dev/consultations/c1/wrong-segment/asr/r1/transcript.json")


def test_parse_artifact_key_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="not one of the closed set"):
        parse_artifact_key("dev/consultations/c1/stages/asr/r1/bogus_kind.json")
