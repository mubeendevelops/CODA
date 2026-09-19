"""Regenerates the graph cassettes in this directory from the fixture
transcripts, asserting every `text_span` is a verbatim substring of the turn
it cites before writing anything.

Run from `python/`:  python nlp-service/tests/fixtures/generate_cassettes.py

The cassettes are hand-authored model responses, not recordings — they are
what a correct model *should* return for each fixture, so a test failing
against them means our code is wrong, not that a model drifted. They are
checked in, and this script exists so editing one cannot silently introduce a
span that does not appear in its transcript: that mistake would make a test
pass against a cassette the real validator would reject.

One file per case, two turns in order: the thought-construction response,
then the edge-prediction response — the same order `graph.pipeline` issues
them in.
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from fixtures.transcripts import (
    cross_turn_symptom_transcript,
    disfluency_transcript,
    interruption_transcript,
    negation_transcript,
)

OUT = pathlib.Path(__file__).resolve().parent / "cassettes"


def T(turn_index, span, text, entities, cat, anchor, pol, conf):
    return {
        "turn_index": turn_index,
        "text_span": span,
        "text": text,
        "entities": entities,
        "clinical_category": cat,
        "temporal_anchor": anchor,
        "polarity": pol,
        "confidence": conf,
    }


def E(src, dst, etype, conf, why):
    return {
        "src_thought_id": src,
        "dst_thought_id": dst,
        "edge_type": etype,
        "confidence": conf,
        "rationale": why,
    }


CASES = {}

# ---------------- negation ----------------
CASES["negation"] = (
    negation_transcript(),
    [
        T(
            1,
            "I've had a sore throat for about four days",
            "Sore throat for approximately four days",
            ["sore throat"],
            "symptom",
            "about four days",
            "asserted",
            1.0,
        ),
        T(3, "A bit, yes", "Mild fever present", ["fever"], "symptom", None, "asserted", 0.8),
        T(
            3,
            "I'm allergic to penicillin",
            "Patient reports a penicillin allergy",
            ["penicillin", "allergy"],
            "allergy",
            None,
            "asserted",
            0.9,
        ),
        T(
            5,
            "Your tonsils are inflamed, there's some exudate",
            "Tonsils inflamed with exudate",
            ["tonsils", "inflammation", "exudate"],
            "examination",
            None,
            "asserted",
            1.0,
        ),
        T(
            7,
            "Possibly",
            "Possible streptococcal pharyngitis",
            ["strep", "pharyngitis"],
            "diagnosis",
            None,
            "uncertain",
            0.7,
        ),
        T(
            9,
            "there's no penicillin allergy documented",
            "No penicillin allergy documented in the medical record",
            ["penicillin", "allergy"],
            "allergy",
            None,
            "negated",
            1.0,
        ),
        T(
            9,
            "the rash you had in 2019 was from a sulfa drug",
            "Rash in 2019 attributed to a sulfa drug",
            ["rash", "sulfa drug"],
            "history",
            "2019",
            "asserted",
            0.9,
        ),
        T(
            10,
            "amoxicillin is safe. Five hundred milligrams three times a day",
            "Amoxicillin 500 mg three times daily prescribed",
            ["amoxicillin"],
            "plan",
            "three times a day",
            "asserted",
            1.0,
        ),
    ],
    [
        E(
            "t3",
            "t6",
            "negation",
            0.95,
            "record review contradicts the reported penicillin allergy",
        ),
        E("t7", "t6", "logical", 0.85, "the sulfa rash explains why the allergy was misattributed"),
        E("t1", "t4", "logical", 0.8, "sore throat is the symptom the tonsil finding examines"),
        E("t4", "t5", "logical", 0.85, "inflamed tonsils with exudate support strep"),
        E("t1", "t5", "logical", 0.8, "sore throat supports the strep working diagnosis"),
        E("t5", "t8", "logical", 0.9, "antibiotic prescribed for the suspected strep"),
        E("t6", "t8", "logical", 0.9, "allergy ruled out, so amoxicillin becomes safe"),
    ],
)

# ---------------- interruption ----------------
CASES["interruption"] = (
    interruption_transcript(),
    [
        T(
            1,
            "It started two days ago, in the middle of my chest",
            "Central chest pain, onset two days ago",
            ["chest pain", "chest"],
            "symptom",
            "two days ago",
            "asserted",
            1.0,
        ),
        T(
            2,
            "It's a sort of pressure",
            "Chest pain is pressure-like in character",
            ["chest pain", "pressure"],
            "symptom",
            None,
            "asserted",
            0.9,
        ),
        T(
            4,
            "Down my left arm",
            "Chest pain radiates down the left arm",
            ["chest pain", "left arm", "radiation"],
            "symptom",
            None,
            "asserted",
            0.8,
        ),
        T(
            4,
            "Mostly when I'm walking uphill",
            "Chest pain is brought on by walking uphill",
            ["chest pain", "exertion"],
            "symptom",
            None,
            "asserted",
            0.9,
        ),
        T(
            6,
            "Yes, after a few minutes",
            "Chest pain eases within a few minutes of rest",
            ["chest pain", "rest"],
            "symptom",
            "after a few minutes",
            "asserted",
            0.85,
        ),
    ],
    [
        E("t1", "t2", "elaboration", 0.95, "adds character to the same chest pain"),
        E("t2", "t3", "elaboration", 0.9, "radiation completes the interrupted description"),
        E("t1", "t3", "elaboration", 0.85, "radiation adds detail to the chest pain"),
        E("t4", "t1", "causal", 0.8, "exertion is named as the trigger for the pain"),
        E("t4", "t5", "elaboration", 0.85, "relief on rest qualifies the exertional pattern"),
    ],
)

# ---------------- disfluency ----------------
CASES["disfluency"] = (
    disfluency_transcript(),
    [
        T(
            1,
            "it's my head, I've been getting these, uh, these headaches, like, at the back here",
            "Headaches located at the back of the head",
            ["headache", "head"],
            "symptom",
            None,
            "asserted",
            0.9,
        ),
        T(
            3,
            "they come on in the, in the afternoon mostly, and then, uh, then they just, they stay",
            "Headaches begin in the afternoon and persist",
            ["headache"],
            "symptom",
            "afternoon",
            "asserted",
            0.8,
        ),
        T(
            5,
            "Since, uh, since Tuesday, no, Monday. It was Monday",
            "Headaches began on Monday",
            ["headache"],
            "symptom",
            "Monday",
            "asserted",
            0.9,
        ),
        T(
            6,
            "I took some, um, some paracetamol, two of the, the five hundreds",
            "Took paracetamol 500 mg, two tablets",
            ["paracetamol"],
            "medication",
            None,
            "asserted",
            0.9,
        ),
        T(
            6,
            "but it didn't, it didn't really do much",
            "Paracetamol gave little relief",
            ["paracetamol"],
            "medication",
            None,
            "negated",
            0.85,
        ),
    ],
    [
        E("t1", "t2", "elaboration", 0.9, "timing adds detail to the same headache"),
        E("t1", "t3", "elaboration", 0.9, "onset date adds detail to the same headache"),
        E("t1", "t4", "logical", 0.8, "paracetamol taken for the headache"),
        E("t4", "t5", "negation", 0.9, "the medication is reported as ineffective"),
    ],
)

# ---------------- cross-turn symptom ----------------
CASES["cross_turn_symptom"] = (
    cross_turn_symptom_transcript(),
    [
        T(
            1,
            "I've got this cough that won't go away",
            "Persistent cough",
            ["cough"],
            "symptom",
            None,
            "asserted",
            1.0,
        ),
        T(
            3,
            "No, nothing like that",
            "No recent travel",
            ["travel"],
            "history",
            None,
            "negated",
            0.9,
        ),
        T(
            5,
            "About three weeks now",
            "Cough has been present for about three weeks",
            ["cough"],
            "symptom",
            "about three weeks",
            "asserted",
            0.9,
        ),
        T(
            7,
            "I gave up eight years ago",
            "Ex-smoker, stopped eight years ago",
            ["smoking"],
            "history",
            "eight years ago",
            "asserted",
            1.0,
        ),
        T(
            9,
            "It's dry, and it's much worse at night — it wakes me up",
            "Cough is dry and worse at night, waking the patient",
            ["cough"],
            "symptom",
            "at night",
            "asserted",
            0.95,
        ),
        T(
            10,
            "I'd like to get a chest X-ray",
            "Chest X-ray requested",
            ["chest x-ray"],
            "investigation",
            None,
            "asserted",
            1.0,
        ),
    ],
    [
        E("t1", "t3", "elaboration", 0.95, "duration adds detail to the same cough"),
        E("t1", "t5", "elaboration", 0.95, "character and nocturnal pattern detail the same cough"),
        E("t3", "t5", "elaboration", 0.85, "both describe the same three-week cough"),
        E("t1", "t6", "logical", 0.9, "persistent cough is the reason for the X-ray"),
        E("t4", "t6", "logical", 0.7, "smoking history strengthens the case for imaging"),
    ],
)

for name, (transcript, thoughts, edges) in CASES.items():
    texts = {t.turn_index: t.text_redacted for t in transcript.turns}
    for i, th in enumerate(thoughts):
        ti = th["turn_index"]
        assert ti in texts, f"{name}: thought {i} cites missing turn {ti}"
        assert th["text_span"] in texts[ti], (
            f"{name}: thought {i} span not verbatim in turn {ti}\n"
            f"  span: {th['text_span']!r}\n  turn: {texts[ti]!r}"
        )
    ids = {f"t{i + 1}" for i in range(len(thoughts))}
    for i, e in enumerate(edges):
        assert e["src_thought_id"] in ids, f"{name}: edge {i} bad src {e['src_thought_id']}"
        assert e["dst_thought_id"] in ids, f"{name}: edge {i} bad dst {e['dst_thought_id']}"
        assert e["src_thought_id"] != e["dst_thought_id"], f"{name}: edge {i} self-loop"

    (OUT / f"{name}.json").write_text(
        json.dumps(
            [
                {
                    "content": json.dumps({"thoughts": thoughts}),
                    "tokens_in": 900,
                    "tokens_out": 450,
                },
                {"content": json.dumps({"edges": edges}), "tokens_in": 400, "tokens_out": 200},
            ],
            indent=2,
        )
        + "\n"
    )
    print(f"{name}: {len(thoughts)} thoughts, {len(edges)} edges — all spans verbatim")
