import json

import pytest

from asr_service import roles
from asr_service.align import TurnDraft
from asr_service.groq_client import GroqCompletionResult
from asr_service.transcribe import WordResult
from coda.v1 import transcript_pb2


def _turn(speaker: str, text: str) -> TurnDraft:
    t = TurnDraft(speaker=speaker)
    t.words.append(WordResult(text=f" {text}", start_s=0.0, end_s=1.0, probability=0.9))
    return t


@pytest.mark.asyncio
async def test_assign_roles_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    turns = [
        _turn("SPEAKER_00", "How long have you had this cough"),
        _turn("SPEAKER_01", "About three days now"),
    ]

    async def fake_chat_json(**kwargs):
        content = json.dumps(
            {
                "clusters": {
                    "SPEAKER_00": {
                        "role": "doctor", "confidence": 0.92, "reasoning": "asks history"
                    },
                    "SPEAKER_01": {
                        "role": "patient", "confidence": 0.88, "reasoning": "reports symptom"
                    },
                }
            }
        )
        return GroqCompletionResult(content=content, tokens_in=120, tokens_out=40)

    monkeypatch.setattr(roles, "chat_json", fake_chat_json)

    assignments, tokens_in, tokens_out = await roles.assign_roles(
        turns, api_key="x", model="m", confidence_threshold=0.6
    )

    assert assignments["SPEAKER_00"].role == transcript_pb2.SpeakerRole.SPEAKER_ROLE_DOCTOR
    assert assignments["SPEAKER_01"].role == transcript_pb2.SpeakerRole.SPEAKER_ROLE_PATIENT
    assert not assignments["SPEAKER_00"].uncertain
    assert tokens_in == 120 and tokens_out == 40


@pytest.mark.asyncio
async def test_assign_roles_flags_duplicate_role_as_uncertain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turns = [_turn("SPEAKER_00", "hello"), _turn("SPEAKER_01", "hi")]

    async def fake_chat_json(**kwargs):
        content = json.dumps(
            {
                "clusters": {
                    "SPEAKER_00": {"role": "doctor", "confidence": 0.7},
                    "SPEAKER_01": {"role": "doctor", "confidence": 0.7},
                }
            }
        )
        return GroqCompletionResult(content=content, tokens_in=1, tokens_out=1)

    monkeypatch.setattr(roles, "chat_json", fake_chat_json)

    assignments, _in, _out = await roles.assign_roles(
        turns, api_key="x", model="m", confidence_threshold=0.6
    )
    assert assignments["SPEAKER_00"].uncertain
    assert assignments["SPEAKER_01"].uncertain


@pytest.mark.asyncio
async def test_assign_roles_low_confidence_is_uncertain(monkeypatch: pytest.MonkeyPatch) -> None:
    turns = [_turn("SPEAKER_00", "hello"), _turn("SPEAKER_01", "hi")]

    async def fake_chat_json(**kwargs):
        content = json.dumps(
            {
                "clusters": {
                    "SPEAKER_00": {"role": "doctor", "confidence": 0.3},
                    "SPEAKER_01": {"role": "patient", "confidence": 0.9},
                }
            }
        )
        return GroqCompletionResult(content=content, tokens_in=1, tokens_out=1)

    monkeypatch.setattr(roles, "chat_json", fake_chat_json)

    assignments, _in, _out = await roles.assign_roles(
        turns, api_key="x", model="m", confidence_threshold=0.6
    )
    assert assignments["SPEAKER_00"].uncertain
    assert not assignments["SPEAKER_01"].uncertain
