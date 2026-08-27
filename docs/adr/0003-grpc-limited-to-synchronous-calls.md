# ADR-0003 — gRPC restricted to fast synchronous calls

- **Status:** Accepted
- **Date:** 2026-08-27
- **Related:** ADR-0002

## Context

ADR-0002 makes Redis Streams the transport for pipeline stages. A small class of calls is genuinely
request/response with a human or a healthcheck waiting on the other end, where routing through a
queue adds latency and complexity for no benefit.

## Decision

gRPC is permitted **only** for bounded synchronous calls, with an explicit deadline on every method:

| Method | Caller → callee | Deadline |
|---|---|---|
| `Health` / `Ready` | orchestrator, Compose healthchecks → both workers | 2 s |
| `GetModelInfo` | go-api → both workers | 2 s |
| `RegenerateField` | go-api → nlp-service | 60 s |
| `EmbedTexts` | nlp-service internal | 10 s |

Any call that could exceed 60 seconds, or whose loss would lose work, goes through Redis Streams.
No pipeline stage is ever invoked over gRPC.

`RegenerateField` is the only sanctioned bypass of the orchestrator. It is permitted because it is
bounded, user-initiated, and does not mutate pipeline state: the result is returned to the UI as a
proposal and persists only if the doctor accepts, at which point it becomes a `review_edit` with
`source = 'regenerated'`. It still writes an audit entry and its tokens are still accounted.

## Consequences

**Positive.** The review UI feels synchronous where a human is waiting. Healthchecks do not depend on
queue liveness, so a stuck queue is diagnosable rather than invisible. Two transports, each with a
crisp rule for which applies.

**Negative.** Two transports to build and maintain, and a second contract surface. `RegenerateField`
creates a path where `go-api` contacts a worker directly, which weakens the topology's simple
dependency story; the audit and non-mutation constraints are what keep it acceptable.

## Alternatives considered

- **Everything over Streams, including regeneration.** Rejected: the UI would poll for a sub-second
  operation, and a queue backlog during an eval sweep would make an interactive feature unusable.
- **Everything over gRPC.** Rejected in ADR-0002.
- **HTTP/JSON instead of gRPC for these calls.** Reasonable and nearly equivalent. gRPC chosen because
  the protobuf contracts already exist (ADR-0004), making the typed client free on both sides.
