# ADR-0010 — MeSH + WHO ICD-10 instead of UMLS/SNOMED-CT

- **Status:** Accepted
- **Date:** 2026-08-27
- **Related:** ADR-0008, decision #9

## Context

GoT-HCS grounds reasoning in UMLS + SNOMED-CT with graph embeddings and cross-attention fusion. UMLS
requires a UTS licence application with multi-day approval, and SNOMED-CT requires an affiliate
licence. The project owner elected to skip UMLS entirely (decision #9).

Knowledge augmentation is nonetheless one of the ablation arms; with nothing in its place, the ablation
would drop an arm and the hallucination-reduction-via-grounding claim would lose its evidence.

## Decision

Substitute **MeSH** (Medical Subject Headings, NLM, freely downloadable, no licence application) as the
concept vocabulary, plus the **WHO ICD-10** list for diagnosis normalization.

Entity linking sits behind a swappable interface with two backends:

- `scispacy_mesh` — scispaCy `en_core_sci_md` + `en_ner_bc5cdr_md` with the MeSH linker (primary)
- `rapidfuzz_mesh` — LLM-NER plus fuzzy matching against a local MeSH descriptor file (fallback)

The backend in use is a `RunConfig` field recorded with every result.

The base paper's MEDCON concept-F1 becomes **MeSH-concept F1**, reported under that name. The
substitution is stated in the methodology chapter, not glossed.

## Consequences

**Positive.** Zero licence, zero application, zero lead time — one dependency and its calendar risk
removed from the critical path. The grounding ablation arm survives. MeSH gives real, curated
biomedical concept identifiers, so grounding is genuine rather than cosmetic. The swappable interface
means assumption A3 (scispaCy's brittle spaCy pins on Python 3.11) cannot block the phase.

**Negative.** MeSH is coarser than UMLS: fewer synonyms, weaker coverage of drug names and procedures,
and no SNOMED-CT clinical granularity. MeSH-concept F1 is **not** numerically comparable to the base
paper's MEDCON scores — only the direction of the ablation effect transfers. Any adoption beyond an
academic project would need to revisit this.

## Alternatives considered

- **Apply for UMLS UTS anyway.** Rejected by decision #9; also adds approval lead time to the critical
  path for a project whose owner wants demonstrable progress quickly.
- **scispaCy's bundled UMLS KB.** Rejected: it is UMLS-derived and carries UMLS licence terms, so it
  does not actually avoid the licence question — it obscures it.
- **Drop knowledge augmentation entirely.** Rejected: costs an ablation arm and the most clinically
  persuasive claim in the project.
- **RxNorm for medications.** Deferred. Complements rather than replaces MeSH; a candidate extension
  if the medications field underperforms.
