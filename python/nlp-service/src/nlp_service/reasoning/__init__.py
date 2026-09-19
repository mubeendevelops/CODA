"""GoT-HCS Modules 5 and 6 — multi-stage reasoning and hierarchical
distillation (claude_context.md §6, plan.md Phase 6).

- `generation` — N candidates per field from N different subgraph views and
  N different instructions, batched one call per variant.
- `scoring` — Eq. 19-22's intent without trained heads: entity-overlap
  relevance, embedding consistency with a contradiction penalty, and raw
  n-gram redundancy. Heuristic (zero API) and LLM-judge backends, selectable
  by run config.
- `embeddings` — MiniLM with a dependency-free lexical fallback, recording
  which one ran.
- `refine` — select the best candidate, then K critique-and-regenerate passes
  with a regression guard.
- `distill` — Module 6: rule-based assembly into the 8 fixed fields with
  turn-id provenance, plus the summary written from the note.
- `store` — every candidate, score and iteration persisted; the discarded
  ones are what the case-study figures are made of.
- `pipeline` — the whole thing, driven entirely by `RunConfig` so every
  ablation arm is configuration rather than a code branch.
"""

from nlp_service.reasoning.distill import FIELD_KEYS, distill_note
from nlp_service.reasoning.generation import generate_candidates
from nlp_service.reasoning.pipeline import ReasoningResult, run_reasoning
from nlp_service.reasoning.refine import refine_field, select_best
from nlp_service.reasoning.scoring import ScoringContext, score_heuristic, score_llm_judge

__all__ = [
    "FIELD_KEYS",
    "ReasoningResult",
    "ScoringContext",
    "distill_note",
    "generate_candidates",
    "refine_field",
    "run_reasoning",
    "score_heuristic",
    "score_llm_judge",
    "select_best",
]
