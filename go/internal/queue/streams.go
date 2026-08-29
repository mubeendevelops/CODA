package queue

import (
	"fmt"

	codev1 "coda/go/internal/genproto/coda/v1"
)

// Stream names, verbatim from docs/architecture.md §2.1's table. They are
// part of the cross-language contract — the Python workers hard-code the
// same strings — so they are constants here, never derived or configurable.
const (
	// StreamASR carries StageEnvelope to asr-service. Producer:
	// go-orchestrator. Consumer group: GroupASRWorkers.
	StreamASR = "stage.asr"
	// StreamNLP carries StageEnvelope to nlp-service, for both the redact
	// and nlp stages — §1.2 gives nlp-service ownership of PII redaction,
	// and §2.1 defines only two request streams, so redaction is dispatched
	// here with StageEnvelope.stage = STAGE_REDACT rather than to a third
	// stream the contract does not define.
	StreamNLP = "stage.nlp"
	// StreamResults carries StageResult from both workers back to the
	// orchestrator. Consumer group: GroupOrchestrator.
	StreamResults = "stage.results"
	// StreamProgress carries StageHeartbeat every 15s while a worker is
	// busy (§2.4). Consumed by the orchestrator only — go-api never reads a
	// stage.* stream (§1.2); progress reaches its SSE endpoint via
	// job_stages (claude_context.md decision #35).
	StreamProgress = "stage.progress"
	// StreamDLQ receives DeadLetter for anything that exceeded max attempts
	// or came back FATAL. §2.1 lists its consumer as "manual / operator":
	// nothing in this system drains it automatically, because a drained DLQ
	// is a silently dropped consultation.
	StreamDLQ = "stage.dlq"

	// StreamJobSubmitted is not a stage.* stream and carries no clinical
	// payload — it is go-api's "a jobs row is ready" doorbell, so a
	// submitted job starts within milliseconds instead of waiting for the
	// orchestrator's next dispatch scan.
	//
	// It is deliberately *not* load-bearing. The jobs row is the durable
	// record (claude_context.md decision #34) and ListDispatchableJobs
	// finds it regardless, so a lost doorbell costs latency, never work.
	// This is also why go-api producing to it does not breach §1.2's
	// prohibition: go-api dispatches no stage and consumes no stream.
	StreamJobSubmitted = "job.submitted"
)

// Consumer group names, also from §2.1's table.
const (
	GroupASRWorkers   = "asr-workers"
	GroupNLPWorkers   = "nlp-workers"
	GroupOrchestrator = "orchestrator"
)

// Redis stream entry field names. protojson lives in one field rather than
// being spread across a flat map so `XRANGE stage.asr - +` prints a
// readable message during debugging — the reason ADR-0004 picked protojson
// over binary protobuf in the first place.
const (
	FieldPayload        = "payload"
	FieldKind           = "kind"
	FieldSchemaVersion  = "schema_version"
	FieldIdempotencyKey = "idempotency_key"
	FieldTraceID        = "trace_id"
	FieldJobID          = "job_id"
	FieldStage          = "stage"
	FieldAttempt        = "attempt"
)

// Message kinds, stamped in FieldKind so a consumer can reject a message of
// the wrong shape loudly instead of unmarshalling protojson into the wrong
// type and getting a zero value.
const (
	KindStageEnvelope  = "StageEnvelope"
	KindStageResult    = "StageResult"
	KindStageHeartbeat = "StageHeartbeat"
	KindDeadLetter     = "DeadLetter"
	KindJobSubmitted   = "JobSubmitted"
)

// SchemaVersion is the envelope + payload contract version stamped on every
// message and checked on receipt (§2.2, §3.1). A worker receiving a version
// it does not implement must fail FATAL with SCHEMA_UNSUPPORTED rather than
// guess. Bump on any breaking payload change.
const SchemaVersion uint32 = 1

// stageNames maps the proto Stage enum to the pipeline_stage domain values
// persisted in Postgres (go/migrations/000010_domains.up.sql). The two
// vocabularies are deliberately different — STAGE_ASR on the wire, 'asr' in
// a column — so the mapping is explicit in one place rather than being a
// strings.ToLower(strings.TrimPrefix(...)) trick scattered around.
var stageNames = map[codev1.Stage]string{
	codev1.Stage_STAGE_ASR:    "asr",
	codev1.Stage_STAGE_REDACT: "redact",
	codev1.Stage_STAGE_NLP:    "nlp",
	codev1.Stage_STAGE_EXPORT: "export",
}

var stagesByName = func() map[string]codev1.Stage {
	m := make(map[string]codev1.Stage, len(stageNames))
	for s, n := range stageNames {
		m[n] = s
	}
	return m
}()

// StageName renders a proto Stage as its pipeline_stage column value.
// STAGE_UNSPECIFIED has no persisted form (the domain's CHECK excludes it
// by design) and yields "".
func StageName(s codev1.Stage) string { return stageNames[s] }

// StageFromName is StageName's inverse, for reading a job_stages row back
// into a message.
func StageFromName(name string) (codev1.Stage, error) {
	s, ok := stagesByName[name]
	if !ok {
		return codev1.Stage_STAGE_UNSPECIFIED, fmt.Errorf("queue: %q is not a pipeline stage", name)
	}
	return s, nil
}

// RequestStreamFor routes a stage to the stream that serves it. asr goes to
// asr-service; redact and nlp both go to nlp-service, which owns redaction
// (§1.2). export has no worker stream — it is produced by go-api after
// doctor approval (§7.5), not dispatched to Python — so it is an error here
// rather than a silent misroute.
func RequestStreamFor(s codev1.Stage) (string, error) {
	switch s {
	case codev1.Stage_STAGE_ASR:
		return StreamASR, nil
	case codev1.Stage_STAGE_REDACT, codev1.Stage_STAGE_NLP:
		return StreamNLP, nil
	case codev1.Stage_STAGE_EXPORT:
		return "", fmt.Errorf("queue: stage export is not dispatched to a worker stream (architecture.md §7.5: export follows doctor approval in go-api)")
	default:
		return "", fmt.Errorf("queue: no request stream for stage %s", s)
	}
}

// GroupFor names the worker consumer group that drains a request stream —
// needed by the orchestrator's reaper, which XAUTOCLAIMs against the
// *workers'* group to reclaim what a dead worker left pending (§2.4).
func GroupFor(stream string) (string, error) {
	switch stream {
	case StreamASR:
		return GroupASRWorkers, nil
	case StreamNLP:
		return GroupNLPWorkers, nil
	case StreamResults, StreamProgress, StreamJobSubmitted:
		return GroupOrchestrator, nil
	default:
		return "", fmt.Errorf("queue: no consumer group for stream %q", stream)
	}
}
