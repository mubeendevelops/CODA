package queue

import (
	"fmt"
	"strconv"

	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"

	codev1 "coda/go/internal/genproto/coda/v1"
)

// marshalOpts is the one protojson configuration used on the wire.
// EmitUnpopulated is off: a zero-valued field means "absent" identically on
// both sides, and omitting them keeps `XRANGE` output readable, which is
// the whole reason ADR-0004 chose protojson over binary protobuf.
var marshalOpts = protojson.MarshalOptions{}

// unmarshalOpts tolerates fields this build does not know about, so a
// worker running a newer minor contract does not hard-fail a message it
// could otherwise process. Genuinely breaking changes are caught by the
// SchemaVersion check instead (§2.2), which is an explicit, loud FATAL
// rather than a silent misread.
var unmarshalOpts = protojson.UnmarshalOptions{DiscardUnknown: true}

// fields renders a proto message into the flat Redis stream entry shape.
// The duplicated scalars (job_id, stage, attempt, idempotency_key,
// trace_id) are denormalised out of the payload on purpose: they make a
// stream entry greppable and let the reaper decide what to do with a
// reclaimed message without unmarshalling it.
func fields(kind string, m proto.Message, extra map[string]string) (map[string]any, error) {
	payload, err := marshalOpts.Marshal(m)
	if err != nil {
		return nil, fmt.Errorf("queue: marshal %s: %w", kind, err)
	}
	out := map[string]any{
		FieldKind:          kind,
		FieldPayload:       string(payload),
		FieldSchemaVersion: strconv.FormatUint(uint64(SchemaVersion), 10),
	}
	for k, v := range extra {
		if v != "" {
			out[k] = v
		}
	}
	return out, nil
}

func envelopeFields(env *codev1.StageEnvelope) (map[string]any, error) {
	return fields(KindStageEnvelope, env, map[string]string{
		FieldJobID:          env.GetJobId(),
		FieldStage:          StageName(env.GetStage()),
		FieldAttempt:        strconv.FormatUint(uint64(env.GetAttempt()), 10),
		FieldIdempotencyKey: env.GetIdempotencyKey(),
		FieldTraceID:        env.GetTraceId(),
	})
}

func resultFields(res *codev1.StageResult) (map[string]any, error) {
	return fields(KindStageResult, res, map[string]string{
		FieldJobID:          res.GetJobId(),
		FieldStage:          StageName(res.GetStage()),
		FieldAttempt:        strconv.FormatUint(uint64(res.GetAttempt()), 10),
		FieldIdempotencyKey: res.GetIdempotencyKey(),
		FieldTraceID:        res.GetTraceId(),
	})
}

func heartbeatFields(hb *codev1.StageHeartbeat) (map[string]any, error) {
	return fields(KindStageHeartbeat, hb, map[string]string{
		FieldJobID:   hb.GetJobId(),
		FieldStage:   StageName(hb.GetStage()),
		FieldAttempt: strconv.FormatUint(uint64(hb.GetAttempt()), 10),
		FieldTraceID: hb.GetTraceId(),
	})
}

func deadLetterFields(dl *codev1.DeadLetter) (map[string]any, error) {
	return fields(KindDeadLetter, dl, map[string]string{
		FieldJobID:   dl.GetJobId(),
		FieldStage:   StageName(dl.GetStage()),
		FieldTraceID: dl.GetOriginalEnvelope().GetTraceId(),
	})
}

// ErrSchemaUnsupported is §2.2's rule made a value: a message whose
// schema_version this build does not implement is rejected, never guessed
// at. The orchestrator turns it into a FATAL / SCHEMA_UNSUPPORTED
// dead-letter rather than a retry, since retrying cannot make an
// unimplemented contract implementable.
var ErrSchemaUnsupported = fmt.Errorf("queue: unsupported schema_version")

// decode unmarshals a message payload into m after checking its kind and
// schema version.
func decode(msg Message, wantKind string, m proto.Message) error {
	if got := msg.Fields[FieldKind]; got != "" && got != wantKind {
		return fmt.Errorf("queue: message %s on %s is a %s, want %s", msg.ID, msg.Stream, got, wantKind)
	}
	if raw := msg.Fields[FieldSchemaVersion]; raw != "" {
		v, err := strconv.ParseUint(raw, 10, 32)
		if err != nil {
			return fmt.Errorf("queue: message %s has unparseable schema_version %q: %w", msg.ID, raw, err)
		}
		if uint32(v) != SchemaVersion {
			return fmt.Errorf("%w: message %s declares %d, this build implements %d", ErrSchemaUnsupported, msg.ID, v, SchemaVersion)
		}
	}
	payload := msg.Fields[FieldPayload]
	if payload == "" {
		return fmt.Errorf("queue: message %s on %s has no %s field", msg.ID, msg.Stream, FieldPayload)
	}
	if err := unmarshalOpts.Unmarshal([]byte(payload), m); err != nil {
		return fmt.Errorf("queue: unmarshal %s from message %s: %w", wantKind, msg.ID, err)
	}
	return nil
}

// DecodeEnvelope reads a StageEnvelope out of a consumed message.
func DecodeEnvelope(msg Message) (*codev1.StageEnvelope, error) {
	env := &codev1.StageEnvelope{}
	return env, decode(msg, KindStageEnvelope, env)
}

// DecodeResult reads a StageResult out of a consumed message.
func DecodeResult(msg Message) (*codev1.StageResult, error) {
	res := &codev1.StageResult{}
	return res, decode(msg, KindStageResult, res)
}

// DecodeHeartbeat reads a StageHeartbeat out of a consumed message.
func DecodeHeartbeat(msg Message) (*codev1.StageHeartbeat, error) {
	hb := &codev1.StageHeartbeat{}
	return hb, decode(msg, KindStageHeartbeat, hb)
}

// DecodeDeadLetter reads a DeadLetter out of a consumed message — used by
// the DLQ alerting job and by operator replay tooling.
func DecodeDeadLetter(msg Message) (*codev1.DeadLetter, error) {
	dl := &codev1.DeadLetter{}
	return dl, decode(msg, KindDeadLetter, dl)
}
