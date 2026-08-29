from asr_service.align import assign_speakers, build_turns
from asr_service.diarize import DiarizedSegment
from asr_service.transcribe import WordResult


def test_assign_speakers_picks_max_overlap() -> None:
    segments = [
        DiarizedSegment(start_s=0.0, end_s=5.0, speaker="SPEAKER_00"),
        DiarizedSegment(start_s=5.0, end_s=10.0, speaker="SPEAKER_01"),
    ]
    words = [
        WordResult(text=" hi", start_s=1.0, end_s=2.0, probability=0.9),
        WordResult(text=" there", start_s=6.0, end_s=7.0, probability=0.9),
    ]
    out = assign_speakers(words, segments)
    assert [w.speaker for w in out] == ["SPEAKER_00", "SPEAKER_01"]


def test_assign_speakers_falls_back_to_nearest_on_gap() -> None:
    segments = [DiarizedSegment(start_s=0.0, end_s=5.0, speaker="SPEAKER_00")]
    # word entirely outside any segment (a diarization gap)
    words = [WordResult(text=" gap", start_s=10.0, end_s=11.0, probability=0.9)]
    out = assign_speakers(words, segments)
    assert out[0].speaker == "SPEAKER_00"


def test_build_turns_splits_on_speaker_change_and_long_pause() -> None:
    words = [
        WordResult(text=" a", start_s=0.0, end_s=1.0, probability=0.9),
        WordResult(text=" b", start_s=1.1, end_s=2.0, probability=0.9),  # same speaker, small gap
        WordResult(text=" c", start_s=10.0, end_s=11.0, probability=0.9),  # same speaker, long gap
    ]
    speakers = ["S0", "S0", "S0"]
    from asr_service.align import WordWithSpeaker

    ws = [WordWithSpeaker(word=w, speaker=s) for w, s in zip(words, speakers, strict=True)]
    turns = build_turns(ws, pause_break_s=1.5)
    assert len(turns) == 2
    assert turns[0].text == "a b"
    assert turns[1].text == "c"


def test_build_turns_splits_on_speaker_change() -> None:
    from asr_service.align import WordWithSpeaker

    words = [
        WordResult(text=" a", start_s=0.0, end_s=1.0, probability=0.9),
        WordResult(text=" b", start_s=1.1, end_s=2.0, probability=0.9),
    ]
    ws = [
        WordWithSpeaker(word=words[0], speaker="S0"),
        WordWithSpeaker(word=words[1], speaker="S1"),
    ]
    turns = build_turns(ws, pause_break_s=1.5)
    assert [t.speaker for t in turns] == ["S0", "S1"]
