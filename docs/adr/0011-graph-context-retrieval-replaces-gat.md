# ADR-0011 — Graph-structured context retrieval replaces the trained GAT

- **Status:** Accepted
- **Date:** 2026-08-27
- **Related:** ADR-0008, ADR-0009, Project_Blueprint.md §7

## Context

GoT-HCS uses a multi-head (8-head) trained Graph Attention Network to propagate information across the
thought graph. Training a GAT requires many graphs with labeled downstream signal; this project has
fewer than 100 conversations, no labels of that kind, no GPU budget, and no fine-tuning in scope
(decision #7).

A panel that has read the base paper will ask specifically about the graph attention component. Quietly
omitting it is the failure mode Project_Blueprint.md §0 identifies as fatal.

## Decision

Replace learned graph attention with **graph-structured context retrieval**, named explicitly as a
substitution in the methodology chapter.

Concretely:

- The thought graph is built with rule-based temporal edges (turn order gives temporal ordering nearly
  free in dialogue — arguably easier than in prose EHR text) and LLM-predicted causal/logical edges.
- For each clinical field, the graph determines **which thoughts enter the generation prompt**: graph
  neighbourhood membership replaces attention-weighted aggregation.
- A **heuristic edge weight** is retained: `weight = entity_overlap × edge_type_prior`. Neighbours are
  ranked and truncated by this weight, so graph structure influences aggregation *strength*, not merely
  membership.
- `graph_context_enabled` is a `RunConfig` field, making this component directly ablatable — the
  `got_k2_nograph` arm isolates exactly its contribution.

## Consequences

**Positive.** No training data, no GPU, no fine-tuning required. The mechanism is inspectable: the
thought graph is serialized per consultation, so "why did the model see these thoughts for this field"
is answerable — which a trained attention matrix would not be. It is directly ablatable, so the project
can state quantitatively what graph structure contributed. It is also the largest token saving in the
system (ADR-0008), since only the relevant neighbourhood enters each prompt rather than the full
transcript.

Retaining a heuristic weight rather than dropping weighting entirely means the honest claim is "graph
structure influences aggregation" rather than the weaker "graph structure selects context".

**Negative.** This is not graph attention and must not be described as such. There is no learned
representation and no multi-head mechanism; edge weights are hand-designed, so any benefit is
attributable to graph structure plus heuristics, not to learned propagation. The project therefore
cannot claim to have validated GoT-HCS's GAT specifically — only its graph-structured reasoning idea.

## Alternatives considered

- **Train a small GAT on available data.** Rejected: fewer than 100 conversations with no labeled
  downstream signal; any result would be noise presented as a finding.
- **Use an untrained randomly-initialised GAT.** Rejected: GAT-shaped for the defence but
  scientifically empty — worse than an honest substitution.
- **Drop graph structure entirely (flat thought list).** Rejected: eliminates the project's primary
  research claim. Retained instead as the `got_k2_nograph` ablation arm, which is where a flat baseline
  belongs.
