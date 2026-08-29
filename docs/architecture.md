# Architecture Specification

CODA — AI-Powered Clinical Documentation & Consultation Summarization Assistant.

Companion documents: `claude_context.md` (project context, decisions), `plan.md` (phases, status),
`docs/adr/` (one ADR per major decision). This document is normative: where it disagrees with the
blueprint, this document wins.

Status: specification complete. Implementation: §2 (transport), §4 (state machine) and §5 are
built (`go/internal/queue`, `go/internal/pipeline`, `go/internal/tasks`); §6 and the Python worker
side of §2 are not.
Last updated: 2026-08-29.

---

## 0. Design drivers

Four constraints shape every decision below. They are not negotiable and each traces to a recorded
decision in `claude_context.md` §7.

| Driver | Consequence |
|---|---|
| **Go is the control plane; Python only where ML libraries force it** | No business logic in Python; no ML in Go. A hard, enforced boundary |
| **Budget is ₹0 — Groq free tier, token-per-day capped** | Token accounting is a first-class concern. Stages must be individually cacheable, resumable, and quota-aware. Quota exhaustion is a normal condition, not an error |
| **The deliverable is a reproducible ablation, not a product** | Every result must be reconstructible from the database alone. Run configuration is versioned, content-hashed, and persisted with every artifact |
| **Solo developer, no ML background, no deadline** | Prefer explicit, inspectable mechanisms over clever ones. An orchestrated state machine beats event choreography; a debuggable pipeline beats a fast one |

---

## 1. Service topology

### 1.1 Services and ports

| Service | Language | Ports | Exposed to |
|---|---|---|---|
| `frontend` | React 18 / Vite / TS | `5173` (dev), `8000` (prod nginx) | browser |
| `go-api` | Go 1.25 | `8080` HTTP/REST, `9090` metrics | browser, operator |
| `go-orchestrator` | Go 1.25 | `8081` health + metrics only | operator only — **no public API** |
| `asr-service` | Python 3.11 | `50051` gRPC, `8082` health | internal network only |
| `nlp-service` | Python 3.11 | `50052` gRPC, `8083` health | internal network only |
| `postgres` | — | `5432` | internal network only |
| `redis` | — | `6379` | internal network only |
| `minio` | — | `9000` S3 API, `9001` console | internal; console operator-only |

Only `frontend` and `go-api` bind to the host in production Compose. Everything else lives on the
internal Docker network. The MinIO console and orchestrator health port are dev conveniences.

### 1.2 Responsibilities and prohibitions

The "must NOT" column is enforced, not advisory. Violations are architecture bugs.

#### `go-api` — REST control plane

**Owns:** authentication (JWT), authorization (RBAC), consent capture and enforcement, multipart
audio upload to MinIO, consultation CRUD, job submission, job-status polling, note retrieval, review
edits, approval, export (JSON/PDF), audit-log writes.

**Must NOT:** run any ML; call Groq or any LLM provider directly; block on pipeline work; consume
from `stage.*` streams; contain prompts or clinical logic; write to `job_stages` (that is the
orchestrator's table).

The single exception to "no direct worker contact" is synchronous single-field regeneration from the
review UI, over gRPC to `nlp-service` — see §2.6.

#### `go-orchestrator` — pipeline state machine

**Owns:** the per-consultation state machine, stage dispatch (`XADD` to `stage.asr` / `stage.nlp`),
result consumption from `stage.results`, retry and backoff policy, quota-park handling, stalled-message
recovery via `XAUTOCLAIM`, DLQ routing, timeout enforcement, cancellation, `jobs` and `job_stages`
persistence.

**Must NOT:** expose a public HTTP API; contain prompts, clinical logic, or field definitions;
call Groq; parse audio or transcripts beyond reading artifact metadata; make decisions that depend on
clinical content (it routes opaque payload references).

#### `asr-service` — Python audio worker

**Owns:** transcription (Groq `whisper-large-v3-turbo`, or local `faster-whisper` int8 — swappable
backend), word-level timestamps, `pyannote.audio` 3.1 diarization, diarization↔word alignment,
speaker-role assignment (Doctor/Patient), transcript assembly into speaker-labeled timestamped turns.

**Must NOT:** know what a clinical field is; know about the thought graph or GoT; write to any table
except its own `job_stages` result row via the result stream; talk to `go-api`.

#### `nlp-service` — Python reasoning worker

**Owns:** PII redaction, thought construction, thought-graph assembly and typed edges, graph-structured
context retrieval, MeSH/ICD-10 entity linking, candidate generation, three-criteria scoring, K-iteration
refinement, hierarchical distillation into the 8 fields, summary generation, LLM response caching.

**Must NOT:** touch audio; perform diarization or ASR; own workflow state; decide retry policy.

#### Infrastructure

| Service | Owns | Must NOT |
|---|---|---|
| `postgres` | All durable state: consultations, jobs, notes, edits, audit, eval, LLM cache | Store large artifacts (blobs go to MinIO) |
| `redis` | Streams (`stage.*`), Asynq queue for Go-internal jobs, ephemeral rate-limit counters | Hold anything that must survive a restart |
| `minio` | Audio, transcripts, thought graphs, candidate sets, PDFs, eval outputs | Be the source of truth for metadata |
| `frontend` | Presentation, review interaction | Talk to anything except `go-api`; hold clinical logic |

### 1.3 Dependency direction

```
browser ──► frontend ──► go-api ──┬──► postgres
                                  ├──► minio
                                  ├──► redis (Asynq only)
                                  └──► nlp-service (gRPC, synchronous, §2.6 only)

go-orchestrator ──┬──► redis (Streams: XADD / XREADGROUP / XAUTOCLAIM)
                  ├──► postgres
                  └──► minio (metadata reads only)

asr-service ──┬──► redis (consumer group on stage.asr)
              ├──► minio (read audio, write transcript)
              └──► Groq Whisper API  [or local faster-whisper]

nlp-service ──┬──► redis (consumer group on stage.nlp)
              ├──► minio (read transcript, write graph/candidates/note)
              ├──► postgres (LLM response cache only)
              └──► Groq chat API (multiple model buckets)
```

No cycles. Python workers never call Go. `go-api` never consumes streams.

---

## 2. Go ↔ Python communication

### 2.1 Default transport: Redis Streams with consumer groups

`go-orchestrator` `XADD`s stage requests to `stage.asr` and `stage.nlp`. Python workers consume via
`XREADGROUP` on a per-service consumer group, and `XADD` results to `stage.results`, which the
orchestrator consumes on its own group. Poison messages route to `stage.dlq`.

| Stream | Producer | Consumer group | Payload |
|---|---|---|---|
| `stage.asr` | go-orchestrator | `asr-workers` | `StageEnvelope` |
| `stage.nlp` | go-orchestrator | `nlp-workers` | `StageEnvelope` |
| `stage.results` | asr-service, nlp-service | `orchestrator` | `StageResult` |
| `stage.progress` | asr-service, nlp-service | `orchestrator` | `StageHeartbeat` |
| `stage.dlq` | go-orchestrator | manual / operator | `DeadLetter` |

One control stream sits outside this table because it carries no clinical payload and is not a
`stage.*` stream:

| Stream | Producer | Consumer group | Payload |
|---|---|---|---|
| `job.submitted` | go-api | `orchestrator` | `{job_id, consultation_id, run_config_id}` |

`job.submitted` is a doorbell, not a dispatch: it lets a newly created `jobs` row start processing in
milliseconds instead of waiting for the orchestrator's next scan. It is deliberately **not**
load-bearing — the `jobs` row is the durable record and `ListDispatchableJobs` finds it regardless, so
a lost doorbell costs latency, never work. This is also why producing to it does not breach §1.2:
go-api dispatches no stage and consumes no stream.

Rationale and rejected alternatives: **ADR-0002**. gRPC scope: **ADR-0003**.

### 2.2 Message envelope

Protobuf-defined, protojson on the wire (**ADR-0004**). Every stage request carries:

```protobuf
message StageEnvelope {
  string   job_id            = 1;  // UUID
  string   consultation_id   = 2;  // UUID
  Stage    stage             = 3;  // STAGE_ASR | STAGE_REDACT | STAGE_NLP | ...
  uint32   attempt           = 4;  // 1-based
  string   idempotency_key   = 5;  // see §2.3
  string   trace_id          = 6;  // W3C traceparent, propagated end-to-end
  uint32   schema_version    = 7;  // envelope + payload contract version
  string   run_config_id     = 8;  // FK to run_configs — pins models, N, K, flags
  string   payload_ref       = 9;  // object-storage URI; NEVER inline data
  google.protobuf.Timestamp enqueued_at = 10;
  google.protobuf.Timestamp deadline    = 11;  // absolute; worker aborts past it
  map<string, string> labels  = 12;  // eval_run_id, arm, org_id — for filtering
}
```

Result messages mirror it:

```protobuf
message StageResult {
  string   job_id          = 1;
  string   consultation_id = 2;
  Stage    stage           = 3;
  uint32   attempt         = 4;
  string   idempotency_key = 5;
  string   trace_id        = 6;
  uint32   schema_version  = 7;
  Status   status          = 8;  // OK | RETRYABLE | FATAL | QUOTA_EXHAUSTED | CANCELLED
  string   result_ref      = 9;  // object-storage URI of the stage output
  Error    error           = 10; // code, message, retryable, provider_status
  StageMetrics metrics     = 11; // tokens_in, tokens_out, llm_calls, cache_hits,
                                 // wall_ms, model_ids[], cost_estimate
  google.protobuf.Timestamp resume_after = 12;  // set when QUOTA_EXHAUSTED
}
```

`schema_version` is checked on receipt. A worker receiving a version it does not implement fails
`FATAL` with `SCHEMA_UNSUPPORTED` rather than guessing — this prevents silent misinterpretation
during migrations.

### 2.3 At-least-once semantics and deduplication

Redis Streams give at-least-once delivery. Duplicates are therefore expected and must be harmless.

**Idempotency key:**

```
idempotency_key = sha256(consultation_id ‖ stage ‖ run_config_id ‖ input_artifact_sha256)
```

It deliberately includes `run_config_id` and the input hash: the same consultation re-processed under
a different arm (baseline vs GoT) is a *different* unit of work, while a redelivered message for the
same arm is the *same* one.

**Dedupe protocol,** executed by the worker before doing any work:

1. `SELECT` from `job_stages` where `idempotency_key = ?` and `status = 'succeeded'`.
2. If found: skip computation, re-emit `StageResult` with the stored `result_ref`, `XACK`, return.
3. Otherwise: claim the row (`INSERT ... ON CONFLICT DO NOTHING` with `status='running'`), work, write
   the artifact, persist the result, then `XACK`.

**Acknowledgement rule:** `XACK` happens **only after** the result is durably persisted in Postgres
and the artifact is committed to MinIO. A crash before `XACK` produces a redelivery that step 2
absorbs. A crash after persistence but before `XACK` costs one redundant no-op delivery — acceptable.

This gives effectively-once *outcomes* over at-least-once *delivery*, without distributed transactions.

### 2.4 Stalled-message recovery

Workers can die mid-stage. Pending entries then sit in the consumer group's PEL forever unless
reclaimed.

- Every worker emits a `StageHeartbeat` to `stage.progress` every **15 s** while working, carrying
  `job_id`, `stage`, `attempt`, `percent_complete`, and a free-text `step` label. The orchestrator —
  the only sanctioned consumer of a `stage.*` stream (§1.2) — lands each one on
  `job_stages.percent_complete` / `.step` / `.heartbeat_at` (migration 000031), which is how progress
  reaches `GET /v1/jobs/{id}` and its SSE variant without go-api ever touching Redis.
- The orchestrator runs a reaper every **30 s**. It issues `XAUTOCLAIM` against each stream for
  entries idle longer than the stage's **visibility timeout**, then re-dispatches with `attempt + 1`.
- Visibility timeout is per-stage, set to roughly 2× observed p95 (§4.2). A job whose heartbeats
  stopped more than 90 s ago is considered stalled even if inside the visibility window.
- Reclaimed messages preserve `trace_id` and `idempotency_key`, so a partially-complete stage that
  already persisted its result is absorbed by the dedupe check rather than recomputed.

### 2.5 Retry, backoff, quota, and poison messages

**Error taxonomy** — the worker classifies; the orchestrator decides.

| Status | Meaning | Orchestrator action |
|---|---|---|
| `OK` | Stage succeeded, `result_ref` valid | Advance the state machine |
| `RETRYABLE` | Transient (network, 5xx, timeout, model overload) | Backoff, `attempt + 1`, re-dispatch |
| `QUOTA_EXHAUSTED` | Daily/minute token or request cap hit | **Park** the job; do **not** increment `attempt` |
| `FATAL` | Malformed input, unsupported schema, unfixable validation failure | Route to `stage.dlq` immediately |
| `CANCELLED` | Cancellation observed mid-stage | Terminal, no retry |

**Backoff:** exponential with full jitter — `delay = random(0, min(max_delay, base × 2^(attempt-1)))`,
`base = 2 s`, `max_delay = 120 s`.

**Max attempts:** `asr` 3, `redact` 2, `nlp` 3, `export` 2. Exceeding it routes to `stage.dlq`.

**Quota parking is not a retry.** This is the single most important operational detail of the whole
system, because on a free tier quota exhaustion is *routine*, not exceptional. On `QUOTA_EXHAUSTED`
the orchestrator moves the job to `*_QUEUED` with `resume_after` set from the provider's
`retry-after`, or to the next quota window if the daily cap was hit. `attempt` is unchanged. A parked
job resumes automatically. Without this rule, a day-cap would burn all three attempts in seconds and
dead-letter every job in the eval run.

**Poison messages:** a `DeadLetter` in `stage.dlq` carries the original envelope, every attempt's
error, the full trace, and the terminal classification. Nothing is silently dropped. Replay is an
explicit operator action that re-`XADD`s with `attempt = 1`.

### 2.6 Where gRPC is used

gRPC is for **fast synchronous calls only** — never for pipeline stages (**ADR-0003**).

| Call | Caller → callee | Deadline | Why synchronous |
|---|---|---|---|
| `Health` / `Ready` | orchestrator, compose healthchecks → both workers | 2 s | Liveness must not queue |
| `GetModelInfo` | go-api → both workers | 2 s | UI shows active model/backend |
| `RegenerateField` | **go-api → nlp-service** | 60 s | Doctor clicks "regenerate" on one field and waits |
| `EmbedTexts` | nlp-service → nlp-service (internal) | 10 s | Local MiniLM lookups in the scorer loop |

`RegenerateField` is the one sanctioned bypass of the orchestrator. It is permitted because it is
bounded, user-initiated, and **does not mutate pipeline state** — the regenerated value is returned to
the UI as a proposal and only persists if the doctor accepts it, at which point it is recorded as a
`review_edit` with `source = 'regenerated'`. It still writes an `audit_log` entry and still consumes
quota, which is accounted against the consultation.

---

## 3. Contracts

### 3.1 Single protobuf source of truth

One protobuf package, `coda.v1`, under `proto/coda/v1/`, is the sole definition of stage payloads,
results, and the clinical note schema. `buf generate` produces Go structs and Python classes.
Hand-writing either side is forbidden (**ADR-0004**).

| File | Defines |
|---|---|
| `common.proto` | `Stage`, `Status`, `Error`, `StageMetrics`, `ArtifactRef` |
| `envelope.proto` | `StageEnvelope`, `StageResult`, `StageHeartbeat`, `DeadLetter` |
| `transcript.proto` | `Word`, `Turn`, `SpeakerCluster`, `Transcript` |
| `thought.proto` | `Thought`, `ThoughtEdge`, `ThoughtGraph` |
| `clinical.proto` | `ClinicalNote` (the 8 fields), `FieldValue`, `Summary` |
| `got.proto` | `Candidate`, `CandidateSet`, `ScoreBreakdown`, `RefinementTrace` |
| `runconfig.proto` | `RunConfig` — the ablation configuration object (§6) |

**Wire format is protojson**, not binary protobuf. The performance difference is irrelevant at this
volume, and human-readable stream contents are worth far more to a solo developer debugging a
multi-service pipeline. Binary remains available for gRPC.

**`schema_version`** is on every message. It is bumped on any breaking change to a payload contract.
Workers reject unknown versions with `FATAL / SCHEMA_UNSUPPORTED`.

`ClinicalNote` carries provenance per field:

```protobuf
message FieldValue {
  string value              = 1;  // null/empty means "not stated" — never a guess
  repeated uint32 source_turn_ids = 2;
  float  confidence         = 3;
  repeated string linked_concepts = 4;  // MeSH / ICD-10 identifiers
}
```

### 3.2 Artifacts by URI, never inline

Messages carry references. Audio, transcripts, thought graphs, candidate sets, and generated notes go
to MinIO (**ADR-0005**). Redis Streams are a control channel, not a data bus — inlining a transcript
would bloat the stream, break the Redis memory budget under an eval sweep, and make replay expensive.

### 3.3 Artifact key naming

Bucket: `coda`. Keys are structured so that immutable source data is shared across every ablation arm,
while config-dependent outputs never collide.

```
# Immutable source — one copy, referenced by every run config
{env}/consultations/{consultation_id}/source/audio/{sha256}.{ext}
{env}/consultations/{consultation_id}/source/consent/{consent_id}.json

# Stage outputs — partitioned by run config, since they vary per arm
{env}/consultations/{consultation_id}/stages/{stage}/{run_config_id}/{artifact_kind}.{ext}

# Evaluation outputs
{env}/eval/{eval_run_id}/{arm}/{consultation_id}/{artifact_kind}.json

# Exports
{env}/consultations/{consultation_id}/exports/{note_version}/{format}.{ext}
```

Concrete examples:

```
dev/consultations/3fa85f64-5717-4562-b3fc-2c963f66afa6/source/audio/sha256-9f2a....wav
dev/consultations/3fa85f64-5717-4562-b3fc-2c963f66afa6/stages/asr/7c9e6679-7425-40de-944b-e07fc1f90ae7/transcript.json
dev/consultations/3fa85f64-5717-4562-b3fc-2c963f66afa6/stages/nlp/7c9e6679-7425-40de-944b-e07fc1f90ae7/thought_graph.json
dev/consultations/3fa85f64-5717-4562-b3fc-2c963f66afa6/stages/nlp/7c9e6679-7425-40de-944b-e07fc1f90ae7/candidate_sets.json
dev/consultations/3fa85f64-5717-4562-b3fc-2c963f66afa6/stages/nlp/7c9e6679-7425-40de-944b-e07fc1f90ae7/clinical_note.json
dev/eval/b2f1a4d0-9c3e-4b7a-8f2d-1e6a9d5c7f31/got_k2/3fa85f64-5717-4562-b3fc-2c963f66afa6/metrics.json
```

Rules: keys are immutable once written (a re-run under a changed config gets a new `run_config_id`,
hence a new key); source audio is content-addressed so identical uploads deduplicate; `artifact_kind`
is drawn from a closed enum in `common.proto`; no PII appears in any key.

---

## 4. Pipeline state machine

Owned exclusively by `go-orchestrator`. Explicit and centralized rather than choreographed
(**ADR-0006**) — with one developer and a research deadline, a state machine you can query with a
single `SELECT` is worth more than the decoupling that event choreography buys.

### 4.1 States and transitions

```
                    ┌──────────────────────────────────────────┐
                    ▼                                          │
CREATED ──► CONSENT_RECORDED ──► UPLOADED ──► ASR_QUEUED ◄──────┤ (quota park /
                                                  │             │  retry)
                                                  ▼             │
                                            ASR_RUNNING ────────┘
                                                  │
                                                  ▼
                                             ASR_DONE
                                                  │
                                                  ▼
                                            REDACT_RUNNING
                                                  │
                                                  ▼
                                              REDACTED
                                                  │
                    ┌─────────────────────────────┤
                    │ (quota park / retry)        ▼
                    └────────────────────► NLP_QUEUED
                                                  │
                                                  ▼
                                            NLP_RUNNING
                                                  │
                                                  ▼
                                              NLP_DONE
                                                  │
                                                  ▼
                                          AWAITING_REVIEW
                                                  │
                                                  ▼
                                            UNDER_REVIEW
                                                  │
                                                  ▼
                                              APPROVED
                                                  │
                                                  ▼
                                              EXPORTED

Terminal off-ramps from any non-terminal state:
   ──► FAILED           (max attempts exceeded, or FATAL)
   ──► DEAD_LETTERED    (poison message parked in stage.dlq)
   ──► CANCELLED        (operator or owner cancellation)
```

`FAILED` and `DEAD_LETTERED` are separated by what an operator can *do* about each.
`DEAD_LETTERED` means a `DeadLetter` carrying the full envelope and failure history reached
`stage.dlq`, so §2.5's replay is available. `FAILED` means a terminal condition with no replayable
message — an unloadable run config, an unroutable stage, or a failure to publish the `DeadLetter`
itself. A `DEAD_LETTERED` job can be recovered from Redis; a `FAILED` one has to be re-submitted.

`CANCELLED` also absorbs consent revocation and DPDP erasure (§7.2), which §4.1 gives no state of
their own: withdrawing consent is the data subject exercising a right — an owner cancellation in this
section's own terms — not a system failure. The distinguishing reason (`cancel_requested`,
`consent_revoked`, `consultation_erased`) is recorded in `jobs.error`.

**Transition rules**

- `CREATED → CONSENT_RECORDED` requires a `consent_records` row. A consultation cannot leave `CREATED`
  without one; enforced by a foreign key and a NOT NULL constraint, not only in application code (§7).
- `ASR_DONE → REDACT_RUNNING` is mandatory and unskippable. **Redaction sits between ASR and NLP by
  design** — it is the last point before transcript text is sent to a third-party LLM (§7.3).
- `NLP_DONE → AWAITING_REVIEW` writes the note with `status = 'draft'`. Nothing is exportable yet.
- `APPROVED` requires an authenticated user holding the `doctor` role. There is **no automatic
  transition into `APPROVED`** — this is the "no auto-save before doctor approval" requirement, and it
  is a DB check constraint as well as an API guard.
- All transitions append to `audit_log` inside the same transaction that performs the transition.

### 4.2 Per-stage timeouts

| Stage | Soft deadline | Visibility timeout (XAUTOCLAIM) | Max attempts |
|---|---|---|---|
| `asr` (hosted Whisper) | 5 min | 10 min | 3 |
| `asr` (local faster-whisper CPU) | 20 min | 40 min | 3 |
| `redact` | 60 s | 120 s | 2 |
| `nlp` (baseline arm) | 10 min | 20 min | 3 |
| `nlp` (GoT arm, K=2) | 45 min | 90 min | 3 |
| `export` | 60 s | 120 s | 2 |

The GoT window is wide because free-tier TPM throttling (8–12K tokens/min) dominates wall-clock: a
~33K-token consultation cannot physically complete faster than roughly 3–4 minutes of pure throttle,
before any model latency. Timeouts are sized against throttle, not compute.

A worker past its `deadline` aborts and returns `RETRYABLE / DEADLINE_EXCEEDED` rather than running on.
The orchestrator enforces the same bound from its side, since a worker that *died* cannot report its
own death: a `running` stage past `deadline_at`, or heartbeat-silent for more than 90 s, is recorded
as `RETRYABLE / DEADLINE_EXCEEDED` (or `HEARTBEAT_LOST`) and re-enters the normal retry/DLQ policy.

**Attempts are counted per stage, not per job.** The ceilings above are per-stage, so a job that spent
two ASR attempts must still get redaction's full two; the counter lives on `job_stages.attempt`, keyed
by idempotency key, and `jobs.attempt` mirrors whichever stage is current.

**Retry and quota parking return a job to the state its stage is dispatched from.** For `asr` and
`nlp` that is the `*_QUEUED` state §2.5 names. `redact` has no `REDACT_QUEUED` state anywhere in
§4.1, so it parks in `ASR_DONE` — the state redaction is dispatched from — which is what makes the
dispatch scan pick it up again when `resume_after` elapses.

### 4.3 Partial failure and resume

Stages are checkpointed at their boundaries: each completed stage's `result_ref` is durable before the
next is dispatched. A crash mid-pipeline resumes at the last incomplete stage — completed stages are
never recomputed, because the dedupe check in §2.3 short-circuits them.

Within the NLP stage, which is long and expensive, there is a second checkpoint layer:

1. Thought construction, graph assembly, and entity linking each write their artifact before the next
   begins. A crash during candidate generation does not re-pay for thought construction.
2. The LLM response cache (`llm_cache`, keyed on `(model, sha256(prompt))`) means even a re-executed
   sub-step costs no tokens if the prompt is unchanged.

Together these make a killed-and-restarted eval run cheap, which §5 of `plan.md` requires as an
acceptance criterion.

### 4.4 Cancellation

Cancellation is cooperative. `go-api` sets `consultations.cancel_requested = true`
(`POST /v1/consultations/{id}/cancel`) and writes an audit entry. The flag is on `consultations`, not
`jobs`, matching the §5.2 table the schema was built from — an earlier draft of this paragraph said
`jobs.cancel_requested`, which no table ever had. Since a consultation's jobs are its ablation arms,
cancelling the consultation cancels every arm, which is what a user clicking "cancel" means.
Workers check the flag at every checkpoint boundary and on each heartbeat tick; on observing it they
abort, emit `CANCELLED`, and `XACK`. The orchestrator moves the consultation to `CANCELLED` and stops
dispatching. In-flight provider calls are not interrupted — already-spent tokens are still accounted.
Artifacts from completed stages are retained (they remain valid inputs for a later re-run).

---

## 5. Data model sketch

Postgres 16. UUIDs (v4, `gen_random_uuid()` via `pgcrypto`) for all primary keys. `created_at` /
`updated_at` on every table. Foreign keys enforced. Migrations via golang-migrate; queries via sqlc.

### 5.1 Tenancy and identity

| Table | Key columns |
|---|---|
| `orgs` | `id`, `name`, `region`, `settings jsonb` |
| `users` | `id`, `org_id → orgs`, `email UNIQUE`, `password_hash` (Argon2id), `role` (`admin` \| `doctor` \| `reviewer` \| `auditor` — claude_context.md decision #30, migration 000027), `active` |
| `consent_records` | `id`, `org_id`, `subject_ref` (pseudonymous, never a name), `consent_type`, `granted_at`, `granted_by → users`, `scope jsonb`, `artifact_uri`, `revoked_at` |
| `refresh_tokens` | `id`, `user_id → users`, `family_id`, `token_hash UNIQUE` (SHA-256 of an opaque token, never the raw value), `issued_at`, `expires_at`, `revoked_at`, `replaced_by_id → refresh_tokens` (rotation chain), `created_by_ip`, `user_agent` — migration 000028; reuse of an already-rotated token (`replaced_by_id` set) revokes the whole `family_id`, not just that row |

### 5.2 Consultations and pipeline

| Table | Key columns |
|---|---|
| `consultations` | `id`, `org_id`, `owner_user_id → users`, **`consent_record_id → consent_records NOT NULL`**, `state` (§4.1 enum), `language` (`en` \| `kn_en`), `source_audio_uri`, `audio_sha256`, `duration_sec`, `cancel_requested bool`, `created_at` |
| `jobs` | `id`, `consultation_id`, `run_config_id → run_configs`, `state`, `current_stage`, `attempt`, `resume_after timestamptz`, `error jsonb`, `trace_id`, `eval_run_id NULL → eval_runs` |
| `job_stages` | `id`, `job_id`, `stage`, **`idempotency_key UNIQUE`**, `status`, `attempt`, `result_ref`, `metrics jsonb` (tokens, llm_calls, cache_hits, wall_ms), `started_at`, `finished_at` |
| `artifacts` | `id`, `consultation_id`, `stage`, `run_config_id`, `kind`, `uri UNIQUE`, `sha256`, `bytes`, `content_type` |

`job_stages.idempotency_key UNIQUE` is the database-level enforcement of §2.3 — the dedupe guarantee
does not depend on application correctness.

### 5.3 Pipeline content

| Table | Key columns |
|---|---|
| `transcripts` | `id`, `consultation_id`, `run_config_id`, `uri`, `asr_backend`, `asr_model`, `wer NULL`, `der NULL`, `language` |
| `turns` | `id`, `transcript_id`, `turn_index`, `speaker_label` (`doctor` \| `patient` \| `unknown`), `start_ms`, `end_ms`, `text`, `text_redacted`, `confidence` |
| `thoughts` | `id`, `consultation_id`, `run_config_id`, `turn_id → turns`, `speaker`, `text`, `entities jsonb`, `category`, `temporal_anchor`, `linked_concepts jsonb` |
| `thought_edges` | `id`, `consultation_id`, `run_config_id`, `src_thought_id`, `dst_thought_id`, `edge_type` (`temporal` \| `causal` \| `logical`), `weight real`, `predicted_by` (`rule` \| `llm`) |
| `extractions` | `id`, `consultation_id`, `run_config_id`, `field_key` (one of the 8), `value jsonb` (`FieldValue`), `candidate_set_uri`, `selected_candidate_idx`, `score_breakdown jsonb`, `iteration` |
| `summaries` | `id`, `consultation_id`, `run_config_id`, `text`, `rouge_l NULL`, `bertscore NULL` |
| `clinical_notes` | `id`, `consultation_id`, `run_config_id`, `version`, `status` (`draft` \| `under_review` \| `approved`), `note jsonb`, `approved_by → users NULL`, `approved_at NULL` |

`extractions` is keyed per field per run config, which is what makes per-field ablation comparison a
plain `GROUP BY` rather than a file-parsing exercise.

### 5.4 Review and audit

| Table | Key columns |
|---|---|
| `reviews` | `id`, `clinical_note_id`, `reviewer_user_id → users`, `started_at`, `completed_at`, `outcome` (`approved` \| `rejected` \| `abandoned`) |
| `review_edits` | `id`, `review_id`, `field_key`, `original_value jsonb`, `edited_value jsonb`, `edit_type` (`correction` \| `addition` \| `deletion` \| `regenerated`), `edited_at`, `editor_user_id` |
| `audit_log` | `id`, `org_id`, `actor_user_id NULL`, `actor_service NULL`, `action`, `resource_type`, `resource_id`, `before jsonb`, `after jsonb`, `trace_id`, `ip inet NULL`, `at timestamptz`, `outcome` (`success` \| `failure` — migration 000029) |

`review_edits` is the HITL data flywheel (decision #14): the original/edited pair per field is exactly
the supervision signal a future fine-tune would need.

`audit_log` is **append-only** — enforced by a `BEFORE UPDATE OR DELETE` trigger that raises, plus
revoking UPDATE/DELETE from the application role.

### 5.5 Evaluation

| Table | Key columns |
|---|---|
| `run_configs` | `id`, **`content_hash UNIQUE`**, `arm`, `config jsonb` (canonical), `schema_version`, `created_at` (§6) |
| `eval_runs` | `id`, `name`, `dataset_split`, `arm`, `run_config_id`, `started_at`, `finished_at`, `status`, `total_tokens`, `total_cost_estimate` |
| `eval_results` | `id`, `eval_run_id`, `consultation_id`, `run_config_id`, `metric_key`, `metric_value double`, `language`, `reference_provenance` (`human_verified` \| `llm_silver` \| `dataset_gold`) |
| `reference_labels` | `id`, `consultation_id`, `field_key`, `value jsonb`, `provenance`, `generated_by_model NULL`, `verified_by_user NULL`, `verified_at NULL` |
| `llm_cache` | `id`, **`(model, prompt_sha256) UNIQUE`**, `response jsonb`, `tokens_in`, `tokens_out`, `created_at`, `hit_count` |

`eval_results.reference_provenance` is mandatory and non-null. Every reported number carries the
provenance of the label it was scored against, so an LLM-silver number can never be mistaken for a
human-verified one when the results chapter is written.

`reference_labels.generated_by_model` exists to enforce decision #3 in code: a check at eval time
asserts `generated_by_model != run_config.base_model`, failing loudly rather than producing circular
metrics (**ADR-0013**).

---

## 6. Ablation architecture

The requirement is that any reported result be reproducible **from the database alone**. That means
configuration cannot live in environment variables, CLI flags, or edited source — it must be a
first-class, versioned, persisted object (**ADR-0012**).

### 6.1 The run-config object

```protobuf
message RunConfig {
  string arm                    = 1;   // baseline | got | got_kg | frontier_ref
  uint32 schema_version         = 2;

  // Models — pinned per role, never inherited from env at runtime
  string base_model             = 3;   // system under test, e.g. llama-3.3-70b-versatile
  string judge_model            = 4;   // e.g. openai/gpt-oss-20b
  string structural_model       = 5;   // e.g. llama-3.1-8b-instant
  string embed_model            = 6;   // e.g. all-MiniLM-L6-v2
  string asr_backend            = 7;   // groq | faster_whisper_local
  string asr_model              = 8;

  // GoT knobs — the ablation dimensions
  bool   got_enabled            = 9;
  uint32 n_candidates           = 10;  // N
  uint32 k_iterations           = 11;  // K
  bool   graph_context_enabled  = 12;  // graph-retrieved context vs full transcript
  bool   kg_enabled             = 13;  // MeSH/ICD-10 augmentation on/off
  string kg_backend             = 14;  // scispacy_mesh | rapidfuzz_mesh | none
  ScorerWeights scorer_weights  = 15;  // relevance / consistency / redundancy

  // Determinism
  float  temperature            = 16;
  float  top_p                  = 17;
  uint32 seed                   = 18;
  string prompt_set_hash        = 19;  // sha256 over the versioned prompt files

  bool   redaction_enabled      = 20;
}
```

`content_hash = sha256(canonical_json(RunConfig))`. Inserting a config that already exists returns the
existing `id` — configs are interned, never duplicated. `run_config_id` is then stamped on `jobs`,
`transcripts`, `thoughts`, `thought_edges`, `extractions`, `summaries`, `clinical_notes`,
`eval_results`, and every artifact key.

### 6.2 The ablation matrix

Each arm is one row of config; nothing else differs:

| Arm | `got_enabled` | `n_candidates` | `k_iterations` | `graph_context` | `kg_enabled` | Purpose |
|---|---|---|---|---|---|---|
| `baseline` | false | 1 | 0 | false | false | **Control** — single-pass, frozen at Phase 4 |
| `got_k1` | true | 3 | 1 | true | false | Refinement-depth ablation |
| `got_k2` | true | 3 | 2 | true | false | **Headline GoT arm** |
| `got_k2_nograph` | true | 3 | 2 | **false** | false | Isolates graph-structured retrieval |
| `got_k2_kg` | true | 3 | 2 | true | **true** | Isolates knowledge augmentation |
| `frontier_ref` | false | 1 | 0 | false | false | Upper reference row, different `base_model` |

`base_model` is identical across `baseline` and every `got_*` arm. Varying it would confound the
experiment; an assertion at eval start enforces this and refuses to run otherwise.

### 6.3 Reproducibility guarantee

Given only a Postgres dump, any result is reconstructible:

```sql
SELECT rc.config, er.metric_key, er.metric_value, er.reference_provenance
FROM eval_results er
JOIN run_configs rc ON rc.id = er.run_config_id
WHERE er.eval_run_id = $1;
```

`config` contains every model ID, every knob, the prompt-set hash, and the seed. Artifact URIs in
`artifacts` resolve to the exact inputs and outputs. `job_stages.metrics` supplies token counts and
latency per stage, which is where the cost-per-consultation figure in `plan.md` Phase 10 comes from.

---

## 7. Security and DPDP Act 2023 posture

India's **Digital Personal Data Protection Act, 2023** governs patient health data here — not HIPAA.
Relevant obligations at the architecture level: lawful consent, purpose limitation, data minimisation,
reasonable security safeguards, breach notification, and erasure on withdrawal of consent.

**Scope statement, recorded honestly:** this system processes **role-play and public-dataset audio
only**. No real patient data is used (decision #1; `plan.md` Phase 12 — v2, formerly Phase 9 — is
explicitly simulated). The
controls below are built because the architecture must be defensible and because the report claims
them — not because live patient data is in scope.

### 7.1 Encryption

**At rest.** MinIO server-side encryption (SSE-S3) on the `coda` bucket, covering audio, transcripts,
graphs, and exports. Postgres: full-instance encryption at the volume level, plus `pgcrypto` column
encryption for the narrow set of columns that can carry identity — `consent_records.subject_ref`,
`redaction_map` payloads, and `users.email`. Encryption keys come from environment-injected secrets in
dev; a real deployment substitutes a KMS, and the key-access interface is written so that swap is a
configuration change.

**In transit.** TLS on every inter-service hop in production, with certificates mounted into the
Compose network. **Dev Compose runs plaintext on the internal Docker network — this is a documented
gap, not an oversight**, and is called out in the report rather than papered over.

### 7.2 Consent enforcement

- `consultations.consent_record_id` is `NOT NULL` with a foreign key. A consultation without consent
  is not representable in the schema.
- The orchestrator refuses to dispatch any stage for a consultation whose consent is missing or whose
  `revoked_at` is set; the check is re-evaluated at every stage dispatch, not only at upload, so a
  mid-pipeline revocation halts processing.
- Consent withdrawal triggers an erasure job: source audio and all derived artifacts are deleted from
  MinIO, content rows are nulled, and a tombstone plus the audit trail are retained (retaining the
  audit record of an erasure is required to evidence compliance).

### 7.3 PII redaction point

**Redaction sits between ASR and NLP** — a mandatory `REDACT_RUNNING` state that cannot be skipped
(§4.1). This is the last moment before transcript text is transmitted to a third-party LLM.

Mechanism: pattern and NER-based detection of names, ages, phone numbers, addresses, and identifiers
in `turns.text`, producing `turns.text_redacted` with stable placeholders (`[PATIENT_NAME]`,
`[PHONE_1]`). The reversible mapping is stored encrypted in a `redaction_map` artifact, so the review
UI can show the doctor the original text while **only redacted text is ever placed in a prompt**.

**A stated limitation, not a hidden one:** with the hosted `asr_backend = groq` the raw audio has
already crossed to a US-based processor before redaction can act. The redaction boundary is fully
effective only under `asr_backend = faster_whisper_local`. This is why local ASR is a first-class
swappable backend rather than a fallback afterthought, and why the run config records which backend
produced every transcript. For role-play data this is acceptable; for real patient data the local
backend would be mandatory. **ADR-0014** records this in full.

### 7.4 Audit logging

Every mutating request and every read of clinical data appends to `audit_log`: actor, action,
resource, org, IP, and outcome (`success`/`failure` — migration 000029 added this column;
`go/internal/audit/middleware.go`). Auth bootstrap actions (register/login/refresh/logout) call the
same `audit.Recorder` directly rather than through the middleware, since they run before a JWT
identity exists to key the middleware off of (`go/internal/http/auth_handlers.go`). The table is
append-only (trigger-enforced; UPDATE/DELETE revoked from the application role). Each entry carries
`trace_id`, joining the audit trail to the distributed trace for any given consultation.

**Partially closed, and worth being precise about which half.** Pipeline state transitions *are*
audited inside the transaction that performs them (`go/internal/pipeline`'s `transition`, which writes
`jobs`, `consultations.state` and `audit_log` in one `pgx.Tx`) — §4.1's requirement, satisfiable there
because the orchestrator already owns a multi-statement write.

go-api's request handlers still are not: the audit write is issued after the action commits, so a
crash in between would leave that one action unaudited. Wiring it through means threading a `pgx.Tx`
from each handler into the recorder, still deferred to when handlers gain real multi-statement writes
(reviews, approvals, export) worth wrapping in a transaction anyway.

**Also not yet true:** `auth.login` for an email that does not exist is not audited (there is no
org to attribute the attempt to without a resolved user row) — a narrow, documented gap, not an
oversight (`go/internal/http/auth_handlers.go`, `Login`).

### 7.5 No auto-save before doctor approval

Enforced at three layers, deliberately redundant:

1. **Schema** — `clinical_notes.status` check constraint: `approved_at` and `approved_by` are non-null
   if and only if `status = 'approved'`.
2. **State machine** — there is no automatic transition into `APPROVED`; the only edge is an explicit
   authenticated action by a `doctor`-role user.
3. **API** — the export endpoint returns `409 Conflict` for any note not in `approved` status.

The pipeline writes `status = 'draft'`. Nothing downstream of the model is treated as clinically valid
without a human in the loop.

### 7.6 Authentication and authorization

JWT access tokens (15 min default, HS256) with opaque refresh tokens (7 days default, rotating,
revocable — `refresh_tokens`, §5.1). Password hashing is Argon2id (`go/internal/auth/password.go`).
Four roles (**claude_context.md decision #30** — supersedes an earlier `doctor`/`admin`/`researcher`
set that shipped with the initial `users` table before any code depended on it):

| Role | May |
|---|---|
| `doctor` | Upload, view, review, edit, approve, export own-org consultations |
| `reviewer` | The same review/edit/approve/view/export set as `doctor`, minus upload — a second clinician in a review workflow |
| `admin` | Everything `doctor`/`reviewer` can, plus user management (registration) and audit-log read |
| `auditor` | Read-only: `audit_log` and de-identified aggregates. **No** access to source audio, unredacted transcripts, or clinical note content |

Enforced at two layers deliberately, not either/or: role membership by route middleware
(`auth.RequireRole`), and org-scoping again at the handler level after the resource is loaded
(`auth.RequireSameOrg`) — every query is parameterized on the caller's own `org_id` from their JWT
claims, never a caller-supplied one, so no cross-org read is expressible through the API. A
cross-org access attempt reports `404`, not `403` — indistinguishable from "does not exist", so
existence in another org is never leaked. Integration-tested adversarially
(`go/internal/http/integration_test.go`, `TestIntegration_OrgScopingCrossOrgReadIsDenied`).

Refresh-token reuse (presenting a token already superseded by rotation) is treated as a compromise
signal and revokes the entire rotation family, not just the reused token.

Rate limiting on upload and auth endpoints (`/v1/auth/*` gets its own, stricter bucket — go-api is
directly exposed to clients in this topology, so the rate limiter keys off the TCP peer address,
stated explicitly via `middleware.ClientIPFromRemoteAddr` rather than a spoofable forwarded-for
header). Secrets via environment injection, never committed; `.env` is git-ignored and
`.env.example` carries only placeholder values; `JWT_SIGNING_KEY` is validated at startup
(minimum length) and never appears in a log line (`config.Auth.LogValue`/`String` redact it).

---

## 8. Observability

- **Tracing:** W3C `trace_id` generated at `go-api` on upload, carried in every envelope, every result,
  every log line, and every `audit_log` row. One consultation is one trace across five services.
- **Metrics:** Prometheus-format on `:9090` (go-api) and `:8081` (orchestrator). Key series — stage
  duration histograms, attempt counts, DLQ depth, quota-park events, `llm_cache` hit ratio, tokens
  consumed per model per day (the operational number that matters most on a free tier).
  Implemented in go-api (`go/internal/http/metrics.go`): `coda_http_requests_total{method,route,status}`
  and `coda_http_request_duration_seconds{method,route}`, on the dedicated metrics port so scraping
  is never subject to the API's own auth or rate limits.
  Implemented in go-orchestrator (`go/internal/pipeline/metrics.go`, served on `:8081/metrics`):
  `coda_pipeline_transitions_total{state}`, `coda_pipeline_dispatches_total{stage}`,
  `coda_pipeline_stage_outcomes_total{stage,status}`, `coda_pipeline_quota_parks_total{stage}`,
  `coda_pipeline_dead_letters_total{stage}`, `coda_pipeline_reclaimed_messages_total{stage}`,
  `coda_pipeline_stage_timeouts_total{stage}`, `coda_pipeline_duplicate_results_total{stage}`,
  `coda_pipeline_cancellations_total{reason}`, plus the `coda_pipeline_dlq_depth` and
  `coda_pipeline_jobs{state}` gauges refreshed by the periodic DLQ-alert job. Quota parks get their
  own counter rather than being folded into stage outcomes because on a free tier they predict an
  eval sweep's wall-clock, and a run that parks forty times a day is rate-limited, not failing —
  indistinguishable in a generic error counter. Per-stage token totals come from
  `job_stages.metrics` rather than a Prometheus series, since they must survive a scrape gap.
- **Logs:** structured JSON, `trace_id` on every line, no PII — redacted text only.

---

## 9. Open items

Tracked as assumptions in `claude_context.md` §9; restated here where they carry architectural risk.

| Item | Architectural consequence if it resolves badly |
|---|---|
| A1 — Whisper WER on Kannada-accented English | If poor, an ASR adaptation stage is inserted between ASR and redaction; the state machine gains a state |
| A2 — pyannote DER/wall-clock on laptop CPU | If too slow, diarization moves to a Colab-executed batch step and `asr` splits into two stages |
| A3 — scispaCy on Python 3.11 | Already mitigated: `kg_backend` is a config field with a `rapidfuzz_mesh` alternative |
| A4 — Groq quota stability | Quota parking (§2.5) absorbs tightening; only wall-clock changes |
