from coda_eval.metrics.summary_metrics import bertscore_batch, bertscore_one, rouge_l


def test_rouge_l_identical_strings_is_perfect() -> None:
    r = rouge_l("patient has a sore throat", "patient has a sore throat")
    assert r.precision == 1.0
    assert r.recall == 1.0
    assert r.fmeasure == 1.0


def test_rouge_l_disjoint_strings_is_zero() -> None:
    r = rouge_l("patient has a sore throat", "completely unrelated words here")
    assert r.fmeasure == 0.0


def test_rouge_l_partial_overlap_between_zero_and_one() -> None:
    r = rouge_l(
        "patient reports sore throat for three days with fever",
        "patient has sore throat for three days",
    )
    assert 0.0 < r.fmeasure < 1.0


def test_bertscore_one_identical_strings_near_one() -> None:
    result = bertscore_one("patient has a sore throat", "patient has a sore throat")
    assert result.f1 > 0.99
    assert result.model == "distilbert-base-uncased"


def test_bertscore_batch_matches_length_and_order() -> None:
    golds = ["patient has a sore throat", "patient reports chest pain"]
    hyps = ["patient has a sore throat", "totally unrelated text about weather"]
    results = bertscore_batch(golds, hyps)
    assert len(results) == 2
    assert results[0].f1 > results[1].f1


def test_bertscore_batch_empty_input() -> None:
    assert bertscore_batch([], []) == []


def test_bertscore_batch_mismatched_lengths_raises() -> None:
    import pytest

    with pytest.raises(ValueError):
        bertscore_batch(["a"], ["a", "b"])
