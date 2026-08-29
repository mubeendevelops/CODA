from coda_eval.normalize import normalize_text


def test_lowercases_and_strips_punctuation() -> None:
    assert normalize_text("Hello, World!") == "hello world"


def test_keeps_word_internal_apostrophe() -> None:
    assert normalize_text("I don't know.") == "i don't know"


def test_strips_edge_apostrophes() -> None:
    assert normalize_text("'quoted' word") == "quoted word"


def test_strips_primock_tags() -> None:
    assert normalize_text("Hello <UNSURE>there</UNSURE> <UNIN/> friend") == "hello there friend"


def test_collapses_whitespace() -> None:
    assert normalize_text("a   b\n\tc") == "a b c"


def test_empty_input() -> None:
    assert normalize_text("") == ""
    assert normalize_text("   ") == ""
