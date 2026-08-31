from nlp_service import prompts


def test_extraction_prompts_load() -> None:
    p = prompts.load_extraction_prompts()
    assert "8" in p.system or "chief_complaint" in p.system
    assert "{transcript_turns}" not in p.system
    assert "transcript_turns" in p.user_template
    assert "validation_error" in p.repair_addendum_template


def test_summary_prompts_load() -> None:
    p = prompts.load_summary_prompts()
    assert p.system
    assert "transcript_turns" in p.user_template


def test_prompt_set_hash_is_deterministic() -> None:
    a = prompts.prompt_set_hash()
    b = prompts.prompt_set_hash()
    assert a == b
    assert len(a) == 64  # sha256 hex digest


def test_prompt_set_hash_changes_language_independently() -> None:
    with_default = prompts.prompt_set_hash(language="en", version="v1")
    assert with_default == prompts.prompt_set_hash()
