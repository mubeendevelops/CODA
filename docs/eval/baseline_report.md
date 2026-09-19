# Baseline (single-pass extraction) evaluation report

**These are the numbers the GoT pipeline (Phase 6) must beat.** Frozen control arm (claude_context.md decision #11), same base model held constant across every arm.

**English-only.** No multilingual (Kannada-English) evaluation has been performed — v1 is English-only by design (claude_context.md §2.1).

## Run metadata

- Timestamp (UTC): 2026-09-05T12:28:15.740484+00:00
- Git SHA: `81607fd3a3e7bc5145beeee6a22bdc9b3e79d98e`
- Base model (system under test): `qwen/qwen3.8-27b`
- Hallucination judge model: `openai/gpt-oss-20b`
- Prompt set hash: `6be37cee3dd6e996f7ec343483420e0e0951ce04eb882c487bab365ecc70f1f7`
- Dataset: PriMock57, 8 gold-annotated consultations (the 8-of-57 subset fetched — decision #61)
- Reference label provenance: `llm_silver` — first-pass gold mapping by Claude Sonnet 5 (a different model than the system under test, decision #3/ADR-0013), **not yet hand-corrected** by the project owner (decision #3's ~15-consultation human-verified subset is a follow-up step, not done this run)

## Caveats

- Hallucination rate was not computed for 2 of 8 consultation(s) (primock57:day1_consultation02, primock57:day1_consultation03) — the judge request still exceeded Groq's free-tier `openai/gpt-oss-20b` TPM cap (8000 tokens/minute) even after restricting to only cited turns (`coda_eval.metrics.hallucination`'s module docstring) and the built-in retry-with-backoff. Every other metric in this report for that item is real. The aggregate hallucination rate above is computed only over items that WERE judged, never treating a missing judgment as either supported or unsupported.
- Reference labels (`data/gold/primock57/*.gold.json`) are `llm_silver` — a first-pass mapping of PriMock57's real clinician notes by Claude Sonnet 5, not yet hand-corrected by a human reviewer. Decision #3's ~15-consultation human-verified subset, and the inter-rater agreement check against it (`coda_eval.metrics.hallucination.inter_rater_agreement`), are both follow-up work, not done this run.

## Headline numbers

- Schema-valid JSON: 8/8 (100.0%)
- Extraction/summary errors (repair budget exhausted or summary too short): 0/8
- Hallucination rate (LLM-judge, all field + summary claims): 0.1786
- Total tokens: 53714 in / 6515 out over 8 consultations (7529 avg/consultation)
- Total wall-clock: 358.3s (44.8s avg/consultation)

## Summarization metrics (vs. gold summary)

| Metric | Precision | Recall | F1 |
|---|---|---|---|
| ROUGE-L | 0.2792 | 0.4537 | 0.3440 |
| BERTScore (`distilbert-base-uncased`) | 0.8363 | 0.8706 | 0.8531 |

## Field-level precision / recall / F1

Matching rule per field documented in `docs/eval/annotation_guide.md` §5. Aggregated as total edits over total opportunities across all consultations (claude_context.md decision #63's rule), never a mean of per-consultation ratios.

| Field | TP | FP | FN | TN | Precision | Recall | F1 | Empty rate | Hallucinated rate |
|---|---|---|---|---|---|---|---|---|---|
| chief_complaint | 7 | 1 | 1 | 0 | 0.8750 | 0.8750 | 0.8750 | 0.0000 |  |
| hopi | 7 | 1 | 1 | 0 | 0.8750 | 0.8750 | 0.8750 | 0.0000 |  |
| examination_findings | 2 | 0 | 0 | 6 | 1.0000 | 1.0000 | 1.0000 | 0.0000 | 0.0000 |
| treatment_plan | 8 | 0 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 | 0.0000 |  |
| past_medical_history | 4 | 12 | 6 | 0 | 0.2500 | 0.4000 | 0.3077 | 0.0000 | 1.0000 |
| medications | 2 | 4 | 2 | 3 | 0.3333 | 0.5000 | 0.4000 | 0.0000 | 0.2500 |
| allergies | 1 | 1 | 1 | 6 | 0.5000 | 0.5000 | 0.5000 | 0.0000 | 0.0000 |
| provisional_diagnosis | 8 | 4 | 2 | 0 | 0.6667 | 0.8000 | 0.7273 | 0.0000 |  |
| investigations_advised | 1 | 2 | 3 | 4 | 0.3333 | 0.2500 | 0.2857 | 0.2500 | 0.0000 |
| **overall (micro)** | 40 | 25 | 16 | 19 | 0.6154 | 0.7143 | 0.6612 | 0.0200 | 0.1364 |

## Per-consultation detail

| Item | Schema valid | Repair attempts | Hallucination rate | Wall (ms) | Error |
|---|---|---|---|---|---|
| primock57:day1_consultation01 | True | 0 | 0.0833 | 7967 |  |
| primock57:day1_consultation02 | True | 0 |  | 47358 |  |
| primock57:day1_consultation03 | True | 0 |  | 51068 |  |
| primock57:day1_consultation04 | True | 0 | 0.2143 | 49704 |  |
| primock57:day1_consultation05 | True | 0 | 0.0588 | 50513 |  |
| primock57:day2_consultation01 | True | 0 | 0.1667 | 51415 |  |
| primock57:day2_consultation02 | True | 0 | 0.5000 | 49965 |  |
| primock57:day3_consultation01 | True | 0 | 0.0667 | 50266 |  |
