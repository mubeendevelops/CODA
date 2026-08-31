import json

from nlp_service.schema import validate_extraction

VALID = {
    "chief_complaint": {"value": "cough", "source_turn_ids": [0], "confidence": 0.9},
    "hopi": None,
    "past_medical_history": [],
    "medications": [],
    "allergies": [],
    "examination_findings": None,
    "provisional_diagnosis": [],
    "investigations_advised": [],
    "treatment_plan": None,
}


def test_valid_extraction_passes() -> None:
    result = validate_extraction(json.dumps(VALID), known_turn_ids={0, 1, 2})
    assert result.valid
    assert result.parsed == VALID


def test_non_json_fails_syntactically() -> None:
    result = validate_extraction("not json at all", known_turn_ids={0})
    assert not result.valid
    assert "not valid JSON" in result.error


def test_missing_required_key_fails_schema() -> None:
    bad = dict(VALID)
    del bad["treatment_plan"]
    result = validate_extraction(json.dumps(bad), known_turn_ids={0})
    assert not result.valid
    assert "schema validation failed" in result.error


def test_extra_top_level_key_fails_schema() -> None:
    bad = dict(VALID)
    bad["unexpected_field"] = "x"
    result = validate_extraction(json.dumps(bad), known_turn_ids={0})
    assert not result.valid


def test_confidence_out_of_range_fails_schema() -> None:
    bad = dict(VALID)
    bad["chief_complaint"] = {"value": "cough", "source_turn_ids": [0], "confidence": 1.5}
    result = validate_extraction(json.dumps(bad), known_turn_ids={0})
    assert not result.valid


def test_unknown_source_turn_id_fails_semantically() -> None:
    bad = dict(VALID)
    bad["chief_complaint"] = {"value": "cough", "source_turn_ids": [99], "confidence": 0.9}
    result = validate_extraction(json.dumps(bad), known_turn_ids={0, 1, 2})
    assert not result.valid
    assert "99" in result.error
    assert "do not exist in the transcript" in result.error


def test_unknown_source_turn_id_in_list_field_fails_semantically() -> None:
    bad = dict(VALID)
    bad["medications"] = [{"value": "paracetamol", "source_turn_ids": [7], "confidence": 0.8}]
    result = validate_extraction(json.dumps(bad), known_turn_ids={0, 1})
    assert not result.valid
    assert "medications[0]" in result.error
