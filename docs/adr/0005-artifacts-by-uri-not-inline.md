# ADR-0005 — Large artifacts pass by object-storage URI, never inline in messages

- **Status:** Accepted
- **Date:** 2026-08-27
- **Related:** ADR-0002, ADR-0012

## Context

Pipeline stages exchange audio (tens of MB), transcripts (100s of KB), thought graphs, candidate sets
(N=3 candidates × 8 fields × K=2 iterations), and generated notes. An evaluation sweep runs this over
20–30 consultations across 6 arms.

## Decision

Redis Streams carry **control messages only**. Every payload larger than a few kilobytes is written to
MinIO and referenced by URI in `payload_ref` / `result_ref`. Artifact keys follow the scheme in
`docs/architecture.md` §3.3.

Source audio is content-addressed (`{sha256}.wav`) so identical uploads deduplicate. Stage outputs are
partitioned by `run_config_id` so ablation arms never collide while sharing one copy of the source.

## Consequences

**Positive.** Redis memory stays bounded regardless of eval sweep size. Replaying a DLQ message is
cheap because the message is small and its inputs still exist. The `run_config_id` partition means the
full input/output set of any arm is enumerable by key prefix, which is what makes the reproducibility
guarantee in ADR-0012 concrete. Immutable keys mean a re-run under changed config cannot overwrite the
evidence for a previously reported number.

**Negative.** Two systems must be consistent: a committed artifact with an unpersisted result row
leaves an orphan. Mitigated by writing the artifact first and treating orphans as garbage-collectable
(never as corruption), and by the acknowledge-after-persist rule. Every worker needs object-storage
credentials.

## Alternatives considered

- **Inline payloads in stream messages.** Rejected: unbounded Redis memory growth and expensive replay.
- **Shared filesystem volume.** Rejected: works in single-host Compose but is not addressable, has no
  content-hash semantics, and would have to be replaced entirely by any non-laptop deployment.
- **Artifacts as Postgres `bytea`.** Rejected: bloats the database, makes dumps unwieldy, and Postgres
  is the metadata source of truth rather than a blob store.
