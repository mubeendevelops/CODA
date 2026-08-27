# Architecture Decision Records

One record per major architectural decision. Format: Status, Date, Context, Decision, Consequences
(positive and negative, stated honestly), Alternatives considered.

Decisions are immutable once accepted. A reversal is a new ADR that supersedes the old one; the
original is not edited.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-go-control-plane-python-ml-services.md) | Go control plane, Python only for ML services | Accepted |
| [0002](0002-redis-streams-for-go-python-transport.md) | Redis Streams with consumer groups as the Go↔Python transport | Accepted |
| [0003](0003-grpc-limited-to-synchronous-calls.md) | gRPC restricted to fast synchronous calls | Accepted |
| [0004](0004-protobuf-single-source-of-truth.md) | One protobuf package as contract source; protojson on the wire | Accepted |
| [0005](0005-artifacts-by-uri-not-inline.md) | Large artifacts pass by object-storage URI, never inline | Accepted |
| [0006](0006-orchestrator-owned-state-machine.md) | Centralized orchestrator-owned state machine, not choreography | Accepted |
| [0007](0007-at-least-once-with-idempotency.md) | At-least-once delivery with database-enforced idempotency | Accepted |
| [0008](0008-token-budget-multi-bucket-model-assignment.md) | Token budget as an architectural constraint; multi-bucket models | Accepted |
| [0009](0009-local-embedding-scorers.md) | Local embedding/lexical scorers for two of three GoT criteria | Accepted |
| [0010](0010-mesh-icd10-instead-of-umls.md) | MeSH + WHO ICD-10 instead of UMLS/SNOMED-CT | Accepted |
| [0011](0011-graph-context-retrieval-replaces-gat.md) | Graph-structured context retrieval replaces the trained GAT | Accepted |
| [0012](0012-versioned-run-config-for-reproducible-ablations.md) | Versioned content-hashed run-config persisted with every result | Accepted |
| [0013](0013-reference-labels-from-a-different-model.md) | Reference labels from a different model than the system under test | Accepted |
| [0014](0014-dpdp-posture-and-pii-redaction-point.md) | DPDP posture; PII redaction placed between ASR and NLP | Accepted |
| [0015](0015-llm-response-cache.md) | Postgres-backed LLM response cache keyed on (model, prompt hash) | Accepted |
| [0016](0016-vertical-slice-before-infrastructure.md) | Build a working vertical slice before the full infrastructure | Accepted |
| [0017](0017-eval-harness-before-got-engine.md) | Build the evaluation harness before the GoT engine | Accepted |

## Decisions that supersede the blueprint

| ADR | Supersedes |
|---|---|
| 0001 | §4 — FastAPI + Celery backend |
| 0010 | §4, §7 — UMLS/SNOMED-CT knowledge augmentation |
| 0011 | §7 — trained multi-head GAT |
| 0017 | §10 — evaluation suite scheduled as Phase 7 of 8 |
