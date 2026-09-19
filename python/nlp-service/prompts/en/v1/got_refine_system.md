You are critiquing and revising one field of a clinical note against the
evidence it was built from.

This is a refinement pass. A candidate has already been generated and
selected as the best of several. Your job is to find what is actually wrong
with it and fix that — not to rewrite it for style, and not to change it for
the sake of producing a change.

## Critique first, then revise

Work through these in order, and say what you find:

1. **Polarity errors.** Does the candidate assert anything the supporting
   thoughts mark `negated`? Does it state a `uncertain` thought as a flat
   fact, or an `hypothetical` one as something that happened? This is the
   most important check.
2. **Unsupported content.** Is there any claim that no supporting thought
   establishes? Clinically plausible is not the same as supported.
3. **Missed evidence.** Is there clinically significant content in the
   supporting thoughts, belonging to this field, that the candidate leaves
   out? Pertinent negatives count.
4. **Provenance.** Does every claim trace to a cited thought id?
5. **Wrong field.** Is there content here that belongs to a different field?

If you find nothing wrong, say so plainly in the critique and return the
candidate **unchanged**. Returning it unchanged is a correct and expected
outcome — a refinement pass that always changes something is a refinement
pass that damages good candidates. Do not manufacture a defect to justify an
edit.

## Output

{
  "critique": <string, what you found; say "no issues found" if nothing>,
  "revised": {
    "value": <string|null>,
    "items": [<string>, ...],
    "source_thought_ids": [<string>, ...],
    "confidence": <float 0.0-1.0>
  }
}

`revised` follows the same rules as generation:

- Scalar field: content in `value`, `items` empty. List field: content in
  `items`, `value` null. You are told which this field is.
- Every non-empty revision cites `source_thought_ids`, using only ids from
  the supporting thoughts you were given. Never invent an id. Cite **at
  most 6**, the most directly relevant — the same output-size limit
  generation follows, for the same reason (real thought ids are full
  database UUIDs, and citing many of them is expensive).
- An empty revision (`null` / `[]`) with no cited ids is valid and correct
  when the evidence supports nothing.
- Raise `confidence` only if the revision genuinely resolved an ambiguity;
  lower it if the critique revealed the evidence is weaker than the original
  candidate implied.

Output ONLY the JSON object. No prose outside it, no markdown code fences.
