from coda_eval.metrics.wer_cer import aggregate_wer_cer, compute_wer_cer


def test_identical_text_is_zero_error() -> None:
    r = compute_wer_cer("The quick brown fox", "The quick brown fox")
    assert r.wer == 0.0
    assert r.cer == 0.0
    assert r.hits == 4


def test_one_substitution() -> None:
    r = compute_wer_cer("The quick brown fox", "The slow brown fox")
    assert r.substitutions == 1
    assert r.wer == 1 / 4


def test_case_and_punctuation_are_normalised_away() -> None:
    r = compute_wer_cer("Hello, World!", "hello world")
    assert r.wer == 0.0


def test_empty_reference_nonempty_hypothesis_is_all_insertions() -> None:
    r = compute_wer_cer("", "hello there")
    assert r.wer == 1.0
    assert r.insertions == 2


def test_empty_reference_and_hypothesis() -> None:
    r = compute_wer_cer("", "")
    assert r.wer == 0.0
    assert r.cer == 0.0


def test_aggregate_weights_by_total_ref_words_not_mean_of_ratios() -> None:
    # item 1: 1 error / 1 ref word -> WER 1.0
    # item 2: 1 error / 9 ref words -> WER ~0.111
    # mean-of-ratios would give ~0.55; total-edits-over-total-words gives 2/10 = 0.2
    r1 = compute_wer_cer("cat", "dog")
    r2 = compute_wer_cer(
        "one two three four five six seven eight nine",
        "one two three four five six seven eight ten",
    )
    agg = aggregate_wer_cer([r1, r2])
    assert abs(agg.wer - 0.2) < 1e-9
