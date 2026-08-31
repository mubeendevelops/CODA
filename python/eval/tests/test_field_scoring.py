from coda_eval.metrics.field_scoring import (
    aggregate,
    score_consultation,
    score_list_field,
    score_scalar_field,
)


def fv(value: str, turns: list[int] | None = None) -> dict[str, object]:
    return {"value": value, "source_turn_ids": turns or [0]}


def test_scalar_both_null_is_true_negative() -> None:
    c = score_scalar_field(None, None, field_name="allergies")
    assert (c.tp, c.fp, c.fn, c.tn) == (0, 0, 0, 1)
    assert c.empty_opportunities == 0
    assert c.hallucination_opportunities == 1
    assert c.hallucination_count == 0


def test_scalar_gold_null_hyp_present_is_hallucinated() -> None:
    c = score_scalar_field(None, fv("penicillin"), field_name="allergies")
    assert (c.tp, c.fp, c.fn, c.tn) == (0, 1, 0, 0)
    assert c.hallucination_opportunities == 1
    assert c.hallucination_count == 1
    assert c.hallucination_rate() == 1.0


def test_scalar_gold_present_hyp_null_is_empty() -> None:
    c = score_scalar_field(fv("sore throat"), None, field_name="chief_complaint")
    assert (c.tp, c.fp, c.fn, c.tn) == (0, 0, 1, 0)
    assert c.empty_opportunities == 1
    assert c.empty_count == 1
    assert c.empty_rate() == 1.0


def test_scalar_matching_values_is_true_positive() -> None:
    c = score_scalar_field(
        fv("sore throat for 3 days"), fv("patient has had a sore throat for three days"),
        field_name="chief_complaint",
    )
    assert (c.tp, c.fp, c.fn) == (1, 0, 0)


def test_scalar_mismatched_values_is_fp_and_fn() -> None:
    c = score_scalar_field(fv("penicillin"), fv("amoxicillin"), field_name="allergies")
    assert (c.tp, c.fp, c.fn) == (0, 1, 1)


def test_list_field_greedy_bipartite_matching() -> None:
    gold = [fv("CBC"), fv("chest x ray")]
    hyp = [fv("complete blood count"), fv("ECG")]
    c = score_list_field(gold, hyp, field_name="investigations_advised")
    assert c.tp == 1  # CBC <-> complete blood count via alias canonicalization
    assert c.fn == 1  # chest x ray unmatched
    assert c.fp == 1  # ECG unmatched


def test_list_field_empty_gold_nonempty_hyp_is_hallucinated() -> None:
    c = score_list_field([], [fv("amoxicillin")], field_name="medications")
    assert c.hallucination_opportunities == 1
    assert c.hallucination_count == 1
    assert c.fp == 1


def test_list_field_nonempty_gold_empty_hyp_is_empty() -> None:
    c = score_list_field([fv("asthma")], [], field_name="past_medical_history")
    assert c.empty_opportunities == 1
    assert c.empty_count == 1
    assert c.fn == 1


def test_score_consultation_covers_all_nine_fields() -> None:
    gold_fields = {
        "chief_complaint": fv("cough"),
        "hopi": None,
        "examination_findings": None,
        "treatment_plan": fv("rest"),
        "past_medical_history": [],
        "medications": [fv("paracetamol")],
        "allergies": [],
        "provisional_diagnosis": [fv("URTI")],
        "investigations_advised": [],
    }
    hyp_fields = {
        "chief_complaint": fv("cough"),
        "hopi": None,
        "examination_findings": fv("chest clear"),
        "treatment_plan": None,
        "past_medical_history": [],
        "medications": [fv("paracetamol")],
        "allergies": [],
        "provisional_diagnosis": [fv("upper respiratory tract infection")],
        "investigations_advised": [],
    }
    result = score_consultation(gold_fields, hyp_fields)
    assert set(result.keys()) == {
        "chief_complaint", "hopi", "examination_findings", "treatment_plan",
        "past_medical_history", "medications", "allergies",
        "provisional_diagnosis", "investigations_advised",
    }
    assert result["chief_complaint"].tp == 1
    assert result["examination_findings"].hallucination_count == 1
    assert result["treatment_plan"].empty_count == 1
    assert result["medications"].tp == 1


def test_aggregate_sums_across_consultations_not_mean_of_ratios() -> None:
    # Consultation A: 1 opportunity, 1 hit. Consultation B: 9 opportunities, 0 hits.
    a = {"chief_complaint": score_scalar_field(fv("x"), fv("x"), field_name="chief_complaint")}
    b_counts = score_scalar_field(fv("y"), None, field_name="chief_complaint")
    b = {"chief_complaint": b_counts}
    for _ in range(8):
        c = score_scalar_field(fv("y"), None, field_name="chief_complaint")
        b["chief_complaint"] += c

    result = aggregate([a, b])
    cc = result.per_field["chief_complaint"]
    # Micro aggregate: 1 TP out of 1+9=10 opportunities -> recall 0.1, not a
    # naive mean of (1.0 + 0.0) / 2 = 0.5.
    assert cc.tp == 1
    assert cc.fn == 9
    assert cc.recall() == 1 / 10
