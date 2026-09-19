You are predicting **typed semantic edges** between clinical thoughts already
extracted from a doctor-patient consultation. You are not extracting new
thoughts and you are not writing a note.

You will be given a numbered list of thoughts. Each carries its id, the turn
it came from, the speaker, its text, its clinical category, and its polarity.
Your job is to say which pairs of thoughts are related, and how.

## Edge types you predict

You predict exactly these five types. You do **not** predict temporal edges —
those are derived by rule from turn order before you are called, because in
dialogue turn order already *is* temporal order. Do not emit them.

- **causal** — the source thought is a cause, trigger, mechanism, or
  physiological explanation of the target. "Started after I ran up the
  stairs" → "chest pain". Direction matters: source causes target.
- **logical** — the source thought is evidence for, or supports the inference
  to, the target. A symptom cluster to the diagnosis it supports; an
  examination finding to the diagnosis it justifies; a diagnosis to the
  investigation ordered to confirm it. Direction: evidence → conclusion.
- **negation** — the source thought is contradicted, denied, retracted, or
  corrected by the target. Use this whenever a thought with polarity
  `negated` denies an earlier `asserted` or `uncertain` thought about the
  same concept, including when the two are spoken by different speakers many
  turns apart. Direction: the asserted/original thought → the negating or
  correcting thought. **Never omit one of these.** A negation left
  unconnected means the final note can state a symptom the consultation
  explicitly ruled out.
- **elaboration** — the target adds detail, qualification, severity,
  duration, or refinement to the same clinical content as the source. This is
  what stitches a symptom described piecemeal across several non-adjacent
  turns back into one picture. Direction: the earlier/more general thought →
  the later/more specific one.
- **coreference** — the two thoughts refer to the *same* clinical entity or
  episode, typically because one used a pronoun or a vague reference ("it",
  "that", "the same thing", "like I said"). Use this when the thoughts are
  about one thing rather than one adding detail to another. Direction: the
  thought introducing the entity → the thought referring back to it.

If two thoughts are related in more than one way, emit the single strongest
relationship, not several. If two thoughts are merely both present in the
same consultation, emit nothing — an edge must be a real, defensible semantic
relation.

## Output shape

Output one JSON object with exactly one top-level key:

{
  "edges": [<Edge>, ...]
}

An Edge is:

{
  "src_thought_id": <string, an id from the thought list>,
  "dst_thought_id": <string, a different id from the thought list>,
  "edge_type": <"causal" | "logical" | "negation" | "elaboration" | "coreference">,
  "confidence": <float between 0.0 and 1.0>,
  "rationale": <string, one short clause saying why>
}

Rules:
- Both ids must appear in the thought list you were given. Never invent one.
- `src_thought_id` and `dst_thought_id` must differ.
- Emit at most one edge per ordered pair.
- `confidence` is how sure you are the relation holds — not how important it
  is. Hedge honestly; a low-confidence edge is downweighted downstream, an
  overconfident wrong one is not.
- `rationale` is one clause, under fifteen words, in plain language.
- Precision over recall. An empty `edges` list is a valid answer for a
  transcript whose thoughts are genuinely unrelated. Spurious edges pull
  irrelevant material into the generation context and directly cause
  hallucination downstream.

## Worked example

Thoughts:

    t1  [turn 2, patient, symptom, asserted]   Chest pain for two days
    t2  [turn 2, patient, symptom, asserted]   Pain started after climbing stairs
    t3  [turn 5, patient, symptom, asserted]   Pain radiates down the left arm
    t4  [turn 6, patient, symptom, negated]    No shortness of breath
    t5  [turn 9, doctor,  diagnosis, asserted] Possible angina
    t6  [turn 11, patient, symptom, negated]   Pain is not related to exertion after all

Output:

{
  "edges": [
    {
      "src_thought_id": "t2",
      "dst_thought_id": "t1",
      "edge_type": "causal",
      "confidence": 0.8,
      "rationale": "exertion named as the trigger for the chest pain"
    },
    {
      "src_thought_id": "t1",
      "dst_thought_id": "t3",
      "edge_type": "elaboration",
      "confidence": 0.95,
      "rationale": "radiation adds detail to the same chest pain"
    },
    {
      "src_thought_id": "t2",
      "dst_thought_id": "t6",
      "edge_type": "negation",
      "confidence": 0.9,
      "rationale": "patient retracts the exertional trigger"
    },
    {
      "src_thought_id": "t1",
      "dst_thought_id": "t5",
      "edge_type": "logical",
      "confidence": 0.85,
      "rationale": "chest pain is evidence for the angina working diagnosis"
    },
    {
      "src_thought_id": "t3",
      "dst_thought_id": "t5",
      "edge_type": "logical",
      "confidence": 0.85,
      "rationale": "radiation to the left arm supports angina"
    }
  ]
}

Note what is absent: no edge from t4, because a denied symptom unrelated to
anything else asserted here connects to nothing, and inventing a link to the
diagnosis would be wrong. Note also t6 → negation of t2 specifically, not of
t1 — the retraction targets the trigger, not the pain itself.

## Output discipline

Output ONLY the JSON object. No prose, no explanation, no markdown code
fences, no trailing commentary.
