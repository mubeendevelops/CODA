//go:build integration

// Transport-level integration tests, against a real Redis. The orchestrator
// suite (internal/pipeline) covers these mechanisms end to end; this file
// covers them in isolation, where a failure points at the transport rather
// than at the state machine sitting on top of it.
//
// Run with: cd go && go test -tags=integration ./internal/queue/...
package queue

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/redis/go-redis/v9"
	tcredis "github.com/testcontainers/testcontainers-go/modules/redis"
	"google.golang.org/protobuf/types/known/timestamppb"

	codev1 "coda/go/internal/genproto/coda/v1"
)

func newTestClient(t *testing.T) (*Client, *redis.Client) {
	t.Helper()
	ctx := context.Background()

	container, err := tcredis.Run(ctx, "redis:7")
	if err != nil {
		t.Fatalf("start redis container: %v", err)
	}
	t.Cleanup(func() {
		if err := container.Terminate(context.Background()); err != nil {
			t.Logf("terminate redis container: %v", err)
		}
	})
	uri, err := container.ConnectionString(ctx)
	if err != nil {
		t.Fatalf("redis connection string: %v", err)
	}
	opts, err := redis.ParseURL(uri)
	if err != nil {
		t.Fatalf("parse redis url: %v", err)
	}
	rdb := redis.NewClient(opts)
	t.Cleanup(func() { _ = rdb.Close() })

	c := NewClientFromRedis(rdb, Options{})
	if err := c.EnsureGroups(ctx); err != nil {
		t.Fatalf("ensure groups: %v", err)
	}
	return c, rdb
}

func testEnvelope(stage codev1.Stage, attempt uint32, key string) *codev1.StageEnvelope {
	return &codev1.StageEnvelope{
		JobId:          uuid.NewString(),
		ConsultationId: uuid.NewString(),
		Stage:          stage,
		Attempt:        attempt,
		IdempotencyKey: key,
		TraceId:        "trace-" + key[:8],
		SchemaVersion:  SchemaVersion,
		RunConfigId:    uuid.NewString(),
		PayloadRef:     "test/consultations/x/source/audio/abc.wav",
		EnqueuedAt:     timestamppb.Now(),
		Deadline:       timestamppb.New(time.Now().Add(5 * time.Minute)),
	}
}

// TestIntegration_EnsureGroupsIsIdempotentAndCreatesMissingStreams covers the
// cold-start property: a consumer group can be created before anything has
// ever been published, so the orchestrator does not have to start *after*
// the first message.
func TestIntegration_EnsureGroupsIsIdempotentAndCreatesMissingStreams(t *testing.T) {
	c, rdb := newTestClient(t)
	ctx := context.Background()

	// Called again — a restart must be a no-op, not a BUSYGROUP error.
	if err := c.EnsureGroups(ctx); err != nil {
		t.Fatalf("EnsureGroups is not idempotent: %v", err)
	}

	for _, stream := range []string{StreamASR, StreamNLP, StreamResults, StreamProgress, StreamJobSubmitted, StreamDLQ} {
		if n, err := rdb.Exists(ctx, stream).Result(); err != nil || n != 1 {
			t.Errorf("stream %s was not created (exists=%d, err=%v)", stream, n, err)
		}
	}
	// stage.dlq has no consumer group by design (§2.1: "manual /
	// operator") — a group would invite something to drain it, and a
	// drained DLQ is a silently dropped consultation.
	groups, err := rdb.XInfoGroups(ctx, StreamDLQ).Result()
	if err != nil {
		t.Fatalf("xinfo groups on %s: %v", StreamDLQ, err)
	}
	if len(groups) != 0 {
		t.Errorf("stage.dlq should have no consumer group, has %d", len(groups))
	}
}

// TestIntegration_PublishGuardSuppressesADuplicateXADD covers the
// producer-side half of ADR-0007. The DB constraint is the guarantee; this
// guard is what stops the duplicate *delivery* that would otherwise have a
// worker claim the message before the constraint absorbed it.
func TestIntegration_PublishGuardSuppressesADuplicateXADD(t *testing.T) {
	c, rdb := newTestClient(t)
	ctx := context.Background()

	key := IdempotencyKey(uuid.New(), codev1.Stage_STAGE_ASR, uuid.New(), "abc")
	env := testEnvelope(codev1.Stage_STAGE_ASR, 1, key)

	first, err := c.PublishEnvelope(ctx, env)
	if err != nil {
		t.Fatalf("first publish: %v", err)
	}
	if first.Deduplicated || first.MessageID == "" {
		t.Fatalf("first publish should have XADDed, got %+v", first)
	}

	second, err := c.PublishEnvelope(ctx, env)
	if err != nil {
		t.Fatalf("second publish: %v", err)
	}
	if !second.Deduplicated {
		t.Error("re-publishing the same (key, attempt) must be suppressed")
	}
	if n := rdb.XLen(ctx, StreamASR).Val(); n != 1 {
		t.Errorf("stage.asr holds %d entries after a duplicate publish, want 1", n)
	}

	// A genuine retry is attempt 2 of the same work unit, and must publish.
	retry := testEnvelope(codev1.Stage_STAGE_ASR, 2, key)
	third, err := c.PublishEnvelope(ctx, retry)
	if err != nil {
		t.Fatalf("retry publish: %v", err)
	}
	if third.Deduplicated {
		t.Error("attempt 2 of the same work unit is a real retry and must publish")
	}
	if n := rdb.XLen(ctx, StreamASR).Val(); n != 2 {
		t.Errorf("stage.asr holds %d entries after a legitimate retry, want 2", n)
	}
}

// TestIntegration_AckOnlyAfterHandlerSuccess is §2.3's acknowledgement rule.
// A handler that reports failure must leave its entry in the PEL, because
// that is what makes the work recoverable rather than silently lost —
// ADR-0007 names getting this wrong as the failure mode that is invisible
// rather than loud.
func TestIntegration_AckOnlyAfterHandlerSuccess(t *testing.T) {
	c, _ := newTestClient(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	env := testEnvelope(codev1.Stage_STAGE_ASR, 1, IdempotencyKey(uuid.New(), codev1.Stage_STAGE_ASR, uuid.New(), "abc"))
	if _, err := c.PublishEnvelope(ctx, env); err != nil {
		t.Fatalf("publish: %v", err)
	}

	var mu sync.Mutex
	var seen int
	consumer := &Consumer{
		Client: c, Stream: StreamASR, Group: GroupASRWorkers, Name: "failing-worker",
		Block: 200 * time.Millisecond, MinIdle: time.Hour, ReclaimEvery: time.Hour,
		Logger: slog.New(slog.NewTextHandler(io.Discard, nil)),
	}
	consumerCtx, stop := context.WithCancel(ctx)
	go func() {
		_ = consumer.Run(consumerCtx, func(ctx context.Context, msg Message) error {
			mu.Lock()
			seen++
			mu.Unlock()
			return errors.New("simulated persistence failure")
		})
	}()

	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		mu.Lock()
		n := seen
		mu.Unlock()
		if n > 0 {
			break
		}
		time.Sleep(25 * time.Millisecond)
	}
	stop()
	time.Sleep(300 * time.Millisecond)

	mu.Lock()
	got := seen
	mu.Unlock()
	if got == 0 {
		t.Fatal("the handler was never called")
	}

	pending, err := c.PendingCount(ctx, StreamASR, GroupASRWorkers)
	if err != nil {
		t.Fatalf("pending count: %v", err)
	}
	if pending != 1 {
		t.Errorf("a failed handler must leave its entry pending, got %d pending", pending)
	}
}

// TestIntegration_XAutoClaimRecoversADeadConsumersEntry is the transport-level
// version of §2.4: an entry a consumer read but never acknowledged must be
// reclaimable by someone else, or that consultation stops dead with no error
// anywhere.
func TestIntegration_XAutoClaimRecoversADeadConsumersEntry(t *testing.T) {
	c, rdb := newTestClient(t)
	ctx := context.Background()

	env := testEnvelope(codev1.Stage_STAGE_NLP, 1, IdempotencyKey(uuid.New(), codev1.Stage_STAGE_NLP, uuid.New(), "abc"))
	if _, err := c.PublishEnvelope(ctx, env); err != nil {
		t.Fatalf("publish: %v", err)
	}

	// A worker reads it and dies.
	if _, err := rdb.XReadGroup(ctx, &redis.XReadGroupArgs{
		Group: GroupNLPWorkers, Consumer: "doomed", Streams: []string{StreamNLP, ">"}, Count: 1,
	}).Result(); err != nil {
		t.Fatalf("simulate worker read: %v", err)
	}
	if n, _ := c.PendingCount(ctx, StreamNLP, GroupNLPWorkers); n != 1 {
		t.Fatalf("expected 1 pending entry, got %d", n)
	}

	// Below the idle threshold, nothing is reclaimed — a worker that is
	// merely slow must keep its message.
	msgs, err := c.ReclaimStale(ctx, StreamNLP, GroupNLPWorkers, "reaper", time.Hour, 16)
	if err != nil {
		t.Fatalf("reclaim (long idle): %v", err)
	}
	if len(msgs) != 0 {
		t.Errorf("reclaimed %d entries inside the visibility window — that steals work from a slow worker", len(msgs))
	}

	time.Sleep(600 * time.Millisecond)

	msgs, err = c.ReclaimStale(ctx, StreamNLP, GroupNLPWorkers, "reaper", 300*time.Millisecond, 16)
	if err != nil {
		t.Fatalf("reclaim (short idle): %v", err)
	}
	if len(msgs) != 1 {
		t.Fatalf("expected to reclaim the dead consumer's entry, got %d", len(msgs))
	}
	if !msgs[0].Reclaimed {
		t.Error("a reclaimed message should be flagged as such")
	}

	// §2.4: reclaimed messages preserve trace_id and idempotency_key, so
	// the dedupe check can still absorb a partially-complete stage.
	got, err := DecodeEnvelope(msgs[0])
	if err != nil {
		t.Fatalf("decode reclaimed envelope: %v", err)
	}
	if got.GetIdempotencyKey() != env.GetIdempotencyKey() {
		t.Errorf("reclaim lost the idempotency key: %q vs %q", got.GetIdempotencyKey(), env.GetIdempotencyKey())
	}
	if got.GetTraceId() != env.GetTraceId() {
		t.Errorf("reclaim lost the trace id: %q vs %q", got.GetTraceId(), env.GetTraceId())
	}
	if got.GetPayloadRef() != env.GetPayloadRef() {
		t.Errorf("reclaim lost the payload ref: %q vs %q", got.GetPayloadRef(), env.GetPayloadRef())
	}
}

// TestIntegration_SchemaVersionMismatchIsRejectedNotGuessed covers §2.2's
// rule: a message declaring a contract version this build does not
// implement must fail loudly rather than be unmarshalled into a
// half-understood value.
func TestIntegration_SchemaVersionMismatchIsRejectedNotGuessed(t *testing.T) {
	c, rdb := newTestClient(t)
	ctx := context.Background()

	if err := rdb.XAdd(ctx, &redis.XAddArgs{
		Stream: StreamResults,
		Values: map[string]any{
			FieldKind:          KindStageResult,
			FieldSchemaVersion: "99",
			FieldPayload:       `{"jobId":"` + uuid.NewString() + `","status":"STATUS_OK"}`,
		},
	}).Err(); err != nil {
		t.Fatalf("xadd: %v", err)
	}

	entries, err := rdb.XRangeN(ctx, StreamResults, "-", "+", 1).Result()
	if err != nil || len(entries) != 1 {
		t.Fatalf("read back: %v (%d entries)", err, len(entries))
	}
	msg := toMessage(StreamResults, GroupOrchestrator, entries[0], false, 0)

	if _, err := DecodeResult(msg); !errors.Is(err, ErrSchemaUnsupported) {
		t.Errorf("expected ErrSchemaUnsupported, got %v", err)
	}
	_ = c
}

// TestIntegration_DeadLetterCarriesFullHistory checks the transport half of
// §2.5's "nothing is silently dropped", and that reading the DLQ does not
// drain it.
func TestIntegration_DeadLetterCarriesFullHistory(t *testing.T) {
	c, _ := newTestClient(t)
	ctx := context.Background()

	env := testEnvelope(codev1.Stage_STAGE_NLP, 3, IdempotencyKey(uuid.New(), codev1.Stage_STAGE_NLP, uuid.New(), "abc"))
	attempts := []*codev1.AttemptError{
		{Attempt: 1, Error: &codev1.Error{Code: "PROVIDER_UNAVAILABLE", Message: "503", Retryable: true}, OccurredAt: timestamppb.Now()},
		{Attempt: 2, Error: &codev1.Error{Code: "PROVIDER_UNAVAILABLE", Message: "503", Retryable: true}, OccurredAt: timestamppb.Now()},
		{Attempt: 3, Error: &codev1.Error{Code: "DEADLINE_EXCEEDED", Message: "no response", Retryable: true}, OccurredAt: timestamppb.Now()},
	}
	if _, err := c.PublishDeadLetter(ctx, DeadLetterRequest{
		Envelope: env, Attempts: attempts, FinalStatus: codev1.Status_STATUS_RETRYABLE,
	}); err != nil {
		t.Fatalf("publish dead letter: %v", err)
	}

	if depth, err := c.DLQDepth(ctx); err != nil || depth != 1 {
		t.Fatalf("dlq depth = %d (err=%v), want 1", depth, err)
	}

	dls, err := c.ReadDeadLetters(ctx, 10)
	if err != nil {
		t.Fatalf("read dead letters: %v", err)
	}
	if len(dls) != 1 {
		t.Fatalf("expected 1 dead letter, got %d", len(dls))
	}
	if n := len(dls[0].GetAttempts()); n != 3 {
		t.Errorf("dead letter carries %d attempts, want 3", n)
	}
	if dls[0].GetOriginalEnvelope().GetIdempotencyKey() != env.GetIdempotencyKey() {
		t.Error("dead letter lost the original envelope's idempotency key — it cannot be replayed")
	}

	// Reading must not consume: §2.1 gives stage.dlq a manual/operator
	// consumer, and a DLQ that drains itself is indistinguishable from one
	// that works.
	if depth, err := c.DLQDepth(ctx); err != nil || depth != 1 {
		t.Errorf("reading the DLQ drained it: depth = %d (err=%v)", depth, err)
	}
}

// TestIntegration_PublishEnvelopeRefusesUnroutableStages guards §7.5 at the
// transport layer: export has no worker stream, so an orchestrator bug that
// tried to dispatch it must fail rather than silently misroute onto
// stage.nlp.
func TestIntegration_PublishEnvelopeRefusesUnroutableStages(t *testing.T) {
	c, rdb := newTestClient(t)
	ctx := context.Background()

	env := testEnvelope(codev1.Stage_STAGE_EXPORT, 1, IdempotencyKey(uuid.New(), codev1.Stage_STAGE_EXPORT, uuid.New(), "abc"))
	if _, err := c.PublishEnvelope(ctx, env); err == nil {
		t.Fatal("publishing an export envelope must fail — export follows doctor approval, not a worker dispatch")
	}
	for _, stream := range []string{StreamASR, StreamNLP} {
		if n := rdb.XLen(ctx, stream).Val(); n != 0 {
			t.Errorf("an unroutable stage leaked %d entries onto %s", n, stream)
		}
	}
}
