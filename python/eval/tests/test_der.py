from pyannote.core import Annotation, Segment

from coda_eval.metrics.der import DerAccumulator, compute_der


def _annotation(segments: list[tuple[float, float, str]], uri: str = "u") -> Annotation:
    ann = Annotation(uri=uri)
    for start, end, speaker in segments:
        ann[Segment(start, end)] = speaker
    return ann


def test_perfect_match_is_zero_der() -> None:
    ref = _annotation([(0, 5, "A"), (5, 10, "B")])
    hyp = _annotation([(0, 5, "X"), (5, 10, "Y")])  # label names don't need to match
    result = compute_der(ref, hyp)
    assert result.der == 0.0
    assert result.missed_detection == 0.0
    assert result.false_alarm == 0.0
    assert result.confusion == 0.0


def test_missed_detection() -> None:
    ref = _annotation([(0, 10, "A")])
    hyp = _annotation([(0, 5, "A")])  # only half detected
    result = compute_der(ref, hyp)
    assert result.missed_detection > 0
    assert result.der > 0


def test_false_alarm() -> None:
    ref = _annotation([(0, 5, "A")])
    hyp = _annotation([(0, 5, "A"), (5, 10, "A")])  # extra speech hypothesised
    result = compute_der(ref, hyp)
    assert result.false_alarm > 0


def test_speaker_confusion() -> None:
    # DER optimally relabels hypothesis speakers per file, so a plain global
    # swap (A<->B) is *not* an error — it's the same partition under a
    # different name. Genuine confusion needs a hypothesis speaker that
    # can't be mapped to more than one reference speaker at once: one
    # hypothesis label covering two reference speakers' time forces exactly
    # one of them to be "confusion" regardless of which way the optimal
    # mapping goes.
    ref = _annotation([(0, 5, "A"), (5, 10, "B")])
    hyp = _annotation([(0, 10, "X")])
    result = compute_der(ref, hyp)
    assert result.confusion > 0
    assert result.der > 0


def test_accumulator_totals_over_mean_of_ratios() -> None:
    acc = DerAccumulator()
    # item 1: 5s of genuine confusion over a 10s reference (see
    # test_speaker_confusion for why a plain relabeling wouldn't count)
    acc.add(_annotation([(0, 5, "A"), (5, 10, "B")]), _annotation([(0, 10, "X")]))
    # item 2: perfect match over 90s
    acc.add(_annotation([(0, 90, "A")]), _annotation([(0, 90, "A")]))
    result = acc.result()
    # total error 5s / total ref 100s = 0.05, not mean(0.5, 0.0) = 0.25
    assert abs(result.der - 0.05) < 1e-6
