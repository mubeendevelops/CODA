# ADR-0012 — Versioned, content-hashed run-config object persisted with every result

- **Status:** Accepted
- **Date:** 2026-08-27
- **Related:** ADR-0005, ADR-0013

## Context

The deliverable is a reproducible ablation, not a product. Results must be defensible months later at a
viva, and the report's central table is a comparison across arms differing only in configuration. If
configuration lives in environment variables, CLI flags, or edited source, a reported number cannot be
traced to the exact conditions that produced it — and with a solo developer iterating over weeks, quiet
config drift between arms is the most likely way the headline result becomes wrong without anyone
noticing.

## Decision

All pipeline configuration lives in a `RunConfig` protobuf message (`docs/architecture.md` §6.1),
persisted in `run_configs` with `content_hash = sha256(canonical_json(config)) UNIQUE`. Configs are
interned: inserting an existing config returns the existing id.

`run_config_id` is stamped on `jobs`, `transcripts`, `thoughts`, `thought_edges`, `extractions`,
`summaries`, `clinical_notes`, `eval_results`, every `artifacts` row, and every artifact storage key.

The config pins every model id per role, `n_candidates`, `k_iterations`, `graph_context_enabled`,
`kg_enabled`, `kg_backend`, scorer weights, temperature, top_p, seed, `prompt_set_hash`, and
`redaction_enabled`. No pipeline behaviour is configurable outside it.

## Consequences

**Positive.** Any reported number is reconstructible from a Postgres dump alone by joining
`eval_results` to `run_configs`. Ablation arms are rows of configuration rather than branches of code,
so adding one is data, not a deploy. Content-hashing makes accidental divergence between two arms
impossible to miss — differing hashes are visible. Artifact keys partitioned by `run_config_id`
(ADR-0005) mean a re-run under changed config cannot overwrite evidence for an already-reported number.
`prompt_set_hash` captures prompt edits, which are otherwise the most common untracked source of drift.

**Negative.** Every new knob requires a proto change, regeneration, and a `schema_version` bump —
deliberate friction that discourages ad-hoc experimentation. Nothing can be tweaked at runtime without
minting a new config, so quick manual experiments cost more.

**Enforced invariant.** `base_model` must be identical across `baseline` and every `got_*` arm; an
assertion at eval start refuses to run otherwise. Varying the model under test between control and
experimental arms would confound the entire result.

## Alternatives considered

- **Environment variables + CLI flags.** Rejected: the standard approach and the standard way ablation
  results become untraceable.
- **YAML config files in git.** Better — versioned and diffable — but does not bind a *result row* to a
  *config version* without a join key. Retained as the human-authoring format that is then interned
  into `run_configs`.
- **Log the config alongside each result.** Rejected: duplicated blobs, no interning, no uniqueness
  guarantee, and no way to query "every result under this exact configuration".
