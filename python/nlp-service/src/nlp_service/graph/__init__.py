"""GoT-HCS Modules 1 and 2, adapted for conversational ASR transcripts
(claude_context.md §6, plan.md Phase 6).

- `thoughts` — Module 1, thought construction: turns to atomic clinical
  thoughts with polarity and sub-turn provenance.
- `edges` — Module 2, graph assembly: rule-derived temporal edges from turn
  order plus LLM-predicted causal/logical/negation/elaboration/coreference
  edges, with heuristic weights.
- `retrieval` — the substitute for the trained GAT: which neighbourhood of
  the graph enters a generation prompt for a given clinical field.
- `pipeline` — Modules 1 and 2 end to end, the seam the rest of Phase 6
  (candidate generation, scoring, refinement, distillation) attaches to.
- `store` — persistence to `thoughts`/`thought_edges` and serialization of
  the whole graph as an artifact, so every report figure is reconstructible.
- `schema` — validation of both LLM calls' JSON output, driving the same
  bounded-repair discipline extraction.py already uses.

Nothing in this package is English-specific: every prompt is loaded through
`nlp_service.prompts` by language, and the rule layer operates on turn
indices and character offsets, not on words (claude_context.md §2.1).
"""

from nlp_service.graph.edges import assemble_edges, temporal_edges
from nlp_service.graph.pipeline import GraphBuildResult, build_thought_graph
from nlp_service.graph.retrieval import RetrievalPolicy, retrieve_field_context
from nlp_service.graph.store import persist_graph, serialize_graph
from nlp_service.graph.thoughts import construct_thoughts

__all__ = [
    "construct_thoughts",
    "build_thought_graph",
    "GraphBuildResult",
    "temporal_edges",
    "assemble_edges",
    "RetrievalPolicy",
    "retrieve_field_context",
    "persist_graph",
    "serialize_graph",
]
