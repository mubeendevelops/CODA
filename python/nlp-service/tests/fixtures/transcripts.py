"""Hand-built fixture transcripts for the dialogue-specific thought-
construction cases hypothesis H3 names (claude_context.md §1, plan.md Phase 6
acceptance criterion 6).

These are written by hand rather than sampled from PriMock57 on purpose: each
one isolates exactly one dialogue phenomenon, so a test that fails names the
phenomenon that broke. Real corpus transcripts mix all four at once and make
a failure ambiguous.

Every fixture is paired with a hand-written expected model response in
`cassettes/`, so the tests exercise the real construction/assembly code
against a fixed model output and never touch the network.
"""

from __future__ import annotations

from coda.v1 import transcript_pb2

D = transcript_pb2.SpeakerRole.SPEAKER_ROLE_DOCTOR
P = transcript_pb2.SpeakerRole.SPEAKER_ROLE_PATIENT


def _transcript(
    turns: list[tuple[int, transcript_pb2.SpeakerRole.ValueType, str]],
    *,
    consultation_id: str = "c-fixture",
    run_config_id: str = "rc-fixture",
) -> transcript_pb2.Transcript:
    return transcript_pb2.Transcript(
        consultation_id=consultation_id,
        run_config_id=run_config_id,
        language="en",
        turns=[
            transcript_pb2.Turn(
                turn_index=idx,
                speaker_label=speaker,
                start_ms=idx * 5000,
                end_ms=(idx + 1) * 5000,
                text=text,
                # The fixtures stand in for post-redaction transcripts, which
                # is what STAGE_NLP actually receives (ADR-0014). Redaction is
                # currently an echo, so text_redacted == text here too.
                text_redacted=text,
                confidence=0.95,
            )
            for idx, speaker, text in turns
        ],
    )


def negation_transcript() -> transcript_pb2.Transcript:
    """Case 1 — cross-turn negation across speakers.

    The patient asserts a penicillin allergy in turn 3; the doctor negates it
    in turn 9, six turns later, having checked the record. The correct output
    is TWO thoughts, not one, linked by a negation edge. A system that keeps
    only the last statement loses the fact that the patient believes they are
    allergic — which is itself clinically relevant — and a system that keeps
    only the first writes a false allergy into the note.
    """
    return _transcript(
        [
            (0, D, "Good morning, what brings you in today?"),
            (1, P, "I've had a sore throat for about four days."),
            (2, D, "Any fever with that?"),
            (3, P, "A bit, yes. And I should say I'm allergic to penicillin."),
            (4, D, "Noted. Let me have a look at your throat."),
            (5, D, "Your tonsils are inflamed, there's some exudate."),
            (6, P, "Is it strep?"),
            (7, D, "Possibly. I'd like to start you on an antibiotic."),
            (8, P, "But the penicillin thing."),
            (
                9,
                D,
                "I've just checked your record and there's no penicillin allergy "
                "documented — the rash you had in 2019 was from a sulfa drug, "
                "not penicillin.",
            ),
            (10, D, "So amoxicillin is safe. Five hundred milligrams three times a day."),
        ]
    )


def interruption_transcript() -> transcript_pb2.Transcript:
    """Case 2 — interruption.

    Turn 2 is cut off mid-sentence ("and it goes"); the doctor interrupts to
    clarify in turn 3, and the patient completes the thought in turn 4. The
    completed clinical fact — that the pain radiates to the left arm — is
    stated by no single turn on its own, and the thought carrying it must be
    anchored to turn 4, the turn where the content was actually said, rather
    than merged into turn 2.
    """
    return _transcript(
        [
            (0, D, "Tell me about the pain."),
            (1, P, "It started two days ago, in the middle of my chest."),
            (2, P, "It's a sort of pressure, and it goes"),
            (3, D, "Sorry — goes where?"),
            (4, P, "Down my left arm. Mostly when I'm walking uphill."),
            (5, D, "Does it ease when you stop?"),
            (6, P, "Yes, after a few minutes."),
        ]
    )


def disfluency_transcript() -> transcript_pb2.Transcript:
    """Case 3 — disfluency-heavy.

    Filler, false starts, self-repair and repetition throughout. The clinical
    content is ordinary; the test is that construction does not lose it, and
    that `text_span` is copied verbatim (disfluency included) while `text`
    is the cleaned assertion. Turn 5 additionally contains a self-repair
    ("Tuesday, no, Monday") — the thought must take the repaired value.
    """
    return _transcript(
        [
            (0, D, "So what's been going on?"),
            (
                1,
                P,
                "It's, um, it's my head, I've been getting these, uh, these headaches, "
                "like, at the back here.",
            ),
            (2, D, "Mm-hmm."),
            (
                3,
                P,
                "And they, they sort of, they come on in the, in the afternoon mostly, "
                "and then, uh, then they just, they stay.",
            ),
            (4, D, "How long has this been happening?"),
            (5, P, "Since, uh, since Tuesday, no, Monday. It was Monday."),
            (
                6,
                P,
                "I took some, um, some paracetamol, two of the, the five hundreds, "
                "but it didn't, it didn't really do much.",
            ),
        ]
    )


def cross_turn_symptom_transcript() -> transcript_pb2.Transcript:
    """Case 4 — one symptom described across three non-adjacent turns.

    The cough is introduced in turn 1, given a duration in turn 5, and given
    a character and a nocturnal pattern in turn 9, with unrelated exchanges
    in between. No turn contains the full picture. Elaboration edges are what
    reassemble it, and graph retrieval for `hopi` must return all three
    despite turns 5 and 9 being four turns apart from their antecedent.
    """
    return _transcript(
        [
            (0, D, "What can I do for you?"),
            (1, P, "I've got this cough that won't go away."),
            (2, D, "Any travel recently?"),
            (3, P, "No, nothing like that."),
            (4, D, "And how long has the cough been there?"),
            (5, P, "About three weeks now."),
            (6, D, "Are you a smoker?"),
            (7, P, "I gave up eight years ago."),
            (8, D, "Is the cough dry, or are you bringing anything up?"),
            (9, P, "It's dry, and it's much worse at night — it wakes me up."),
            (10, D, "Right. I'd like to get a chest X-ray."),
        ]
    )


__all__ = [
    "negation_transcript",
    "interruption_transcript",
    "disfluency_transcript",
    "cross_turn_symptom_transcript",
]
