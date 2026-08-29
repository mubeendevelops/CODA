import tempfile
from pathlib import Path

from coda_eval.textgrid import non_empty_intervals, parse_intervals, strip_transcript_tags

_SAMPLE = """File type = "ooTextFile"
Object class = "TextGrid"

xmin = 0
xmax = 10
tiers? <exists>
size = 1
item []:
    item [1]:
        class = "IntervalTier"
        name = "Doctor"
        xmin = 0
        xmax = 10
        intervals: size = 3
        intervals [1]:
            xmin = 0
            xmax = 2.5
            text = ""
        intervals [2]:
            xmin = 2.5
            xmax = 5.0
            text = "Hello, how are you <UNSURE>today</UNSURE>?"
        intervals [3]:
            xmin = 5.0
            xmax = 10.0
            text = ""
"""


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "sample.TextGrid"
    p.write_text(content, encoding="utf-8")
    return p


def test_parse_intervals_reads_all_including_empty() -> None:
    with tempfile.TemporaryDirectory() as d:
        path = _write(Path(d), _SAMPLE)
        intervals = parse_intervals(str(path))
        assert len(intervals) == 3
        assert intervals[0].text == ""
        assert intervals[1].start_s == 2.5
        assert intervals[1].end_s == 5.0
        assert "Hello" in intervals[1].text


def test_non_empty_intervals_filters_silence() -> None:
    with tempfile.TemporaryDirectory() as d:
        path = _write(Path(d), _SAMPLE)
        intervals = non_empty_intervals(str(path))
        assert len(intervals) == 1
        assert intervals[0].text.strip().startswith("Hello")


def test_strip_transcript_tags() -> None:
    assert strip_transcript_tags("Hello <UNSURE>there</UNSURE> friend") == "Hello there friend"
    assert strip_transcript_tags("Um <UNIN/> yeah") == "Um  yeah"
