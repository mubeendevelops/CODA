from asr_service.transcribe import WordResult, _build_chunks, _merge_chunk_words


def test_build_chunks_short_audio_is_one_chunk() -> None:
    assert _build_chunks(duration_s=60.0, chunk_length_s=300.0, overlap_s=5.0) == [(0.0, 60.0)]


def test_build_chunks_long_audio_overlaps() -> None:
    chunks = _build_chunks(duration_s=700.0, chunk_length_s=300.0, overlap_s=5.0)
    assert chunks[0] == (0.0, 300.0)
    assert chunks[1] == (295.0, 595.0)
    assert chunks[-1][1] == 700.0
    # consecutive chunks overlap by exactly overlap_s
    for a, b in zip(chunks, chunks[1:], strict=False):
        assert a[1] - b[0] == 5.0


def test_merge_chunk_words_dedupes_overlap_region() -> None:
    # Overlap region is [295, 300); its midpoint (the cut boundary) is 297.5.
    bounds = [(0.0, 300.0), (295.0, 595.0)]
    chunk0 = [
        WordResult(text=" one", start_s=290.0, end_s=291.0, probability=0.9),
        WordResult(text=" two", start_s=296.0, end_s=297.0, probability=0.9),  # before boundary
    ]
    chunk1 = [
        WordResult(  # also before boundary
            text=" two", start_s=296.5, end_s=297.5, probability=0.95
        ),
        WordResult(text=" three", start_s=299.0, end_s=300.0, probability=0.99),  # after boundary
        WordResult(text=" four", start_s=310.0, end_s=311.0, probability=0.9),
    ]
    merged = _merge_chunk_words([chunk0, chunk1], bounds, overlap_s=5.0)
    texts = [w.text.strip() for w in merged]
    assert texts == ["one", "two", "three", "four"]
    # the overlap word came from chunk0 (its start_s is below the 297.5 boundary)
    assert merged[1].probability == 0.9


def test_merge_chunk_words_single_chunk_keeps_everything() -> None:
    bounds = [(0.0, 10.0)]
    words = [WordResult(text=" hi", start_s=0.0, end_s=1.0, probability=1.0)]
    merged = _merge_chunk_words([words], bounds, overlap_s=5.0)
    assert merged == words
