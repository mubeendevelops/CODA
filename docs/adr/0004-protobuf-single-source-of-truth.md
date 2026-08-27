# ADR-0004 — One protobuf package as the contract source of truth; protojson on the wire

- **Status:** Accepted
- **Date:** 2026-08-27
- **Related:** ADR-0001, ADR-0002

## Context

Go and Python must agree on stage payloads, results, the transcript structure, the thought graph, the
run configuration, and the 8-field clinical note schema. Hand-maintained types on both sides drift,
and drift in the clinical note schema would silently corrupt evaluation results.

## Decision

A single protobuf package `coda.v1` under `proto/coda/v1/` defines every cross-service type.
`buf generate` produces Go structs and Python classes. Hand-writing either side is forbidden;
`make generate` is checked in CI-equivalent (`make lint`).

**protojson is the wire format on Redis Streams**, not binary protobuf. Binary is used for gRPC.

Every message carries `schema_version`. A worker receiving an unimplemented version fails
`FATAL / SCHEMA_UNSUPPORTED` rather than attempting a best-effort parse.

## Consequences

**Positive.** One definition of the clinical note schema, used by the API, both workers, the eval
harness, and the frontend's generated types. Schema changes are a single edit plus regeneration.
Human-readable stream payloads make `XRANGE` a usable debugging tool — worth more here than the bytes
protojson costs. Explicit version rejection converts a silent misparse into a loud failure.

**Negative.** A protobuf toolchain (`buf`, `protoc-gen-go`, `grpcio-tools`) is required for
development. protojson payloads are several times larger than binary — irrelevant at this volume, but
it is a real tradeoff being made knowingly. Protobuf's optional/default-value semantics require care:
the clinical note distinguishes "not stated" from "empty", so `FieldValue` uses explicit presence
rather than relying on zero values.

## Alternatives considered

- **JSON Schema + code generation.** Rejected: good for validation, weaker for typed cross-language
  structs, and gRPC (ADR-0003) wants protobuf anyway.
- **Hand-written types on both sides.** Rejected: guaranteed drift; the clinical note schema is the
  one place where drift would corrupt results invisibly.
- **Binary protobuf on the wire.** Rejected: the size win is irrelevant at this volume and the
  debuggability loss is significant for a solo developer.
- **OpenAPI as the single source.** Retained for the public REST surface only; it does not describe
  internal stage payloads well.
