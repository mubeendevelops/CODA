# Gold annotation guide

Companion to `claude_context.md` (decisions #3, #3b, #71) and `plan.md` Phase 5. This document is
the human-facing counterpart to `python/eval/src/coda_eval/gold_schema.py` (the machine-enforced
schema) and `python/eval/src/coda_eval/metrics/field_match.py` (the per-field matching rules) — read
those two files if you want the exact enforced shape rather than the prose description below.

---

## 1. What a gold file is

One gold file per consultation, at `data/gold/<dataset>/<session_id>.gold.json`. It is the answer
key a hypothesis (a real single-pass or GoT extraction run) is scored against: the 8 clinical fields
+ summary, mapped from ground truth, plus per-turn speaker labels.

Per decision #3, gold provenance comes from one of three sources, in order of preference:

1. **`dataset_gold`** — PriMock57 ships real clinician-written notes (`data/raw/primock57/notes/*.json`).
   Mapping one of these onto the 8-field JSON is the preferred path for every PriMock57 item: genuine
   human ground truth, zero extra annotation cost (decision #3b).
2. **`llm_silver`** (first pass) — an LLM *other than the system under test* (`qwen/qwen3.8-27b`)
   reads the note + transcript and proposes the mapping. This project uses Claude (Anthropic), a
   different vendor and architecture than the Groq-hosted system under test, satisfying ADR-0013's
   circularity guard without a second API integration.
3. **`human_verified`** — ~15 consultations (decision #3's number) get the LLM-silver first pass
   hand-corrected by the project owner and re-saved. This is the subset serious claims in the report
   lean on; the rest stay `llm_silver`/`dataset_gold` and are reported as such, never silently
   presented as human-verified.

The gold file itself does not carry a `provenance` field — that lives in `reference_labels`/
`eval_results.reference_provenance` (architecture.md §5.5) once a gold file backs an actual DB-recorded
run. The gold JSON is the content; provenance is metadata about how that content was produced.

---

## 2. Turn numbering

`turn_id` is the 0-indexed line number in the dataset's reference transcript file — one
`"Speaker: text"` line per turn, chronologically ordered (`coda_eval.gold.parse_reference_transcript`).
This is **the same numbering** `nlp_service`'s extraction prompt assigns as `turn_index` when the eval
runner feeds it the identical transcript text (`coda_eval.gold.transcript_turns_text` produces the
exact `turn_index: speaker: text` block the extraction prompt expects) — so a gold `source_turn_ids`
and a hypothesis `source_turn_ids` are directly comparable without any translation step. Never
renumber turns by hand; always regenerate from `parse_reference_transcript` if the underlying
transcript file changes.

---

## 3. Speaker labels

Every turn gets exactly one speaker label: `"Doctor"`, `"Patient"`, or `"Unknown"`. `"Unknown"` is a
legal, expected value — some turns are genuinely ambiguous even against the dataset's own
ground-truth per-channel audio (crosstalk, inaudible audio, a turn that isn't really speech). Record
that honestly rather than guessing; a forced guess here would corrupt any speaker-accuracy metric
computed against it later. `coda-eval gold-scaffold` pre-fills this from PriMock57's own per-channel
TextGrids (free, not re-annotated by hand — the channel a turn came from already tells you who spoke),
so for PriMock57 items you are checking/correcting the scaffold, not labeling from scratch.

---

## 4. The 8 fields + summary

Same taxonomy as `claude_context.md` §3, with two schema differences from the persisted `ClinicalNote`
worth knowing before you annotate:

- **`medications` and `allergies` are separate top-level fields** in the gold schema (and in
  `nlp_service`'s extraction output), not nested under one `medications_allergies` object the way
  §3's table describes the final persisted shape. This matches the field-matching code's per-field
  rule table, which needs the two apart (allergies gets the strictest match rule of any field — see
  §5).
- **Gold field values have no `confidence`.** A human annotation either records a value from the note
  and cites the turns backing it, or it doesn't — there is no partial-confidence gold. `confidence`
  only exists on a hypothesis's `FieldValue`.

Every non-null scalar field, and every item in a list field, MUST carry `source_turn_ids`: the turn(s)
in the reference transcript that support that specific value. This is not optional bookkeeping — it
is what makes the hallucination judge's *field* claims citation-checkable at all (§7). If you cannot
point to a specific turn (or turns) that state the value, either the value doesn't belong in the gold
file, or you need to look again at the transcript before adding it.

**Absent information is `null` (scalar) or `[]` (list), never a guess.** The clinician note frequently
won't mention something the 8-field taxonomy asks for (e.g. no allergies stated) — record that as
empty, not as an inferred "NKDA" unless the transcript or note actually says so.

`summary` is a single non-empty string — a human paraphrase of the note's overall gist, written by
you (or lightly edited from the LLM-silver first pass), not lifted verbatim from the note's `note`
text field. It has no per-sentence turn citations (see §7 for how that affects hallucination scoring).

---

## 5. Field-matching rules (how a gold value and a hypothesis value are compared)

Every field is scored by exactly one rule, chosen for that field's typical content and documented in
`field_match.py`'s module docstring — reproduced here as the human-facing version:

| Field | Rule | Why |
|---|---|---|
| `chief_complaint` | semantic (threshold 0.60) | Free-text narrative; paraphrase is common ("sore throat" vs. "throat pain for 3 days") |
| `hopi` | semantic (0.55) | Longest, most diffuse narrative field — lower threshold since a longer correct paraphrase pair naturally scores lower cosine similarity |
| `examination_findings` | semantic (0.55) | Same reasoning as `hopi` |
| `treatment_plan` | semantic (0.55) | Same reasoning |
| `provisional_diagnosis` | semantic (0.65) | Shorter than the narrative fields, so a tighter threshold; clinical synonymy ("URTI" vs. "upper respiratory tract infection") still needs semantic matching, not exact |
| `past_medical_history` | normalized (rapidfuzz ratio ≥ 85) | Short, fairly canonical noun phrases where only formatting/word-order varies |
| `medications` | normalized (≥ 85) | Same reasoning — dose/frequency notation varies but the drug name doesn't need a semantic model |
| `investigations_advised` | normalized (≥ 90, plus a fixed alias table: "CBC" ≡ "complete blood count" ≡ "full blood count", "CXR" ≡ "chest x ray", "ECG" ≡ "EKG" ≡ "electrocardiogram", "MSU" ≡ "midstream urine", "urine dip" ≡ "urine dipstick") | Short abbreviations are edit-distance-close to each other (CBC vs. CMP), so alias canonicalization runs first and fuzzy matching is the fallback, not the primary signal |
| `allergies` | **exact** (case/whitespace-normalized only, no fuzz) | Safety-critical: a fuzzy false-match between two short strings ("penicillin" vs. "amoxicillin") is exactly the failure mode a clinical eval must not paper over with a lenient rule |

Semantic matching uses local `sentence-transformers/all-MiniLM-L6-v2` embeddings (cosine similarity) —
zero API cost, consistent with the project's token-budget discipline (§8). All thresholds are also
in `field_match.SEMANTIC_THRESHOLDS`/`NORMALIZED_THRESHOLD` as the single source of truth if this
table and the code ever drift.

**List fields are scored by greedy bipartite matching**: each gold item is matched against the first
unmatched hypothesis item the field's rule accepts, in gold order. A short list where order rarely
matters for correctness makes greedy equivalent to an optimal assignment in practice; this is not
claimed for arbitrarily long lists.

---

## 6. Workflow: scaffold, fill, validate

1. `coda-eval gold-scaffold --dataset primock57 --item <session_id>` writes an empty-but-valid-shape
   gold file: real speaker labels (free, from the per-channel TextGrights), every field `null`/`[]`,
   `summary` empty. This is the starting point — it will fail validation until you fill it in.
2. Open the reference transcript (`coda_eval.gold.parse_reference_transcript`'s source file — the
   same "Speaker: text" per-line file `coda-eval registry` already produces) alongside the clinician
   note, and fill in each field's `value` + `source_turn_ids`, following §4-§5 above.
3. `coda-eval gold-validate <path>` (or `--all` for every file under `data/gold/`) runs
   `gold_schema.validate_gold_file`: JSON-Schema shape, every speaker `turn_id` unique and covering
   `range(n_turns)` exactly, and every field's `source_turn_ids` referencing a turn_id that actually
   exists. Fix every reported error before treating the file as usable — a gold file that fails
   validation is not silently coerced into a usable shape anywhere downstream.

---

## 7. Hallucination judge rubric (summary)

The LLM-judge hallucination score (`coda_eval.metrics.hallucination`, full rubric at
`python/eval/prompts/en/v1/hallucination_judge_system.md`) is a **separate** metric from field-level
P/R/F1 above — it does not need gold at all. It checks whether the transcript, at exactly a claim's
cited turn(s), actually supports that specific claim's content: right answer, wrong citation still
scores UNSUPPORTED, since downstream provenance tooling trusts the citation.

**Field claims** are judged against only their own cited turns. **Summary claims** (one per sentence)
have no per-sentence citation in the current schema (§4), so each is instead judged against the
**union of every turn any field claim in that consultation cited** — a coarser, topic-level check
rather than the field check's citation-level one, falling back to every turn in the transcript only
when there were no field claims to build that union from. This asymmetry is intentional, not a gap:
it is the honest reflection of what the two outputs actually carry.

The judge is never sent the full transcript — only whichever turns at least one claim actually cites.
This was tightened from an earlier "send the whole transcript" design after that version hit Groq's
free-tier `openai/gpt-oss-20b` TPM cap (8000 tokens/minute) on every one of the first 8 real
consultations run: a real PriMock57 transcript (80-140 turns) alone exceeds that budget before any
claims or rubric text are added. Restricting to cited turns keeps a real request small without
weakening the check — citation-faithfulness is a claim against ITS OWN cited turns, so a turn nothing
cites was never going to change a verdict anyway.

---

## 8. Human-verified subset and inter-rater agreement

Decision #3's ~15-consultation human-verified subset doubles as the inter-rater agreement check for
the hallucination judge (`coda_eval.metrics.hallucination.inter_rater_agreement`). To contribute a
consultation to this subset:

1. Run the judge over that consultation's claims (or read its logged verdicts from a completed eval
   run).
2. For a sample of those claims (or all of them), independently decide supported/unsupported yourself
   by reading the cited turn(s), *before* looking at the judge's verdict or rationale.
3. Save your ratings as a JSON list at (by convention) `data/gold/hallucination_ratings/<item_id>.json`:
   `[{"claim_id": "field:chief_complaint", "supported": true}, ...]` — same `claim_id` values the
   judge used (`field:<name>` for scalar fields, `field:<name>:<idx>` for list items,
   `summary:<idx>` for summary sentences), loaded by `coda_eval.metrics.hallucination.load_human_ratings`.

`inter_rater_agreement` reports a simple agreement rate and Cohen's kappa over whichever `claim_id`s
appear in both your ratings and the judge's — kappa is `None` when either rater's verdicts are
constant (e.g. every claim rated `supported`), since the chance-agreement term is undefined there,
not because of a bug.
