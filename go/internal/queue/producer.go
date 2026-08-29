package queue

import (
	"context"
	"fmt"
	"time"

	"github.com/redis/go-redis/v9"

	codev1 "coda/go/internal/genproto/coda/v1"
)

// PublishResult reports what a publish actually did.
type PublishResult struct {
	// MessageID is the Redis stream entry ID, empty when Deduplicated.
	MessageID string
	// Deduplicated is true when this exact (stream, idempotency_key,
	// attempt) triple was already published and the XADD was skipped.
	Deduplicated bool
}

// PublishEnvelope XADDs a stage request, guarded by producer-side
// idempotency.
//
// The guard is a `SET NX` on (stream, idempotency_key, attempt) and it
// solves a specific problem: the orchestrator persists a transition and
// then publishes, so a crash *between* those two steps leaves a job whose
// row says dispatched. The restarted orchestrator's dispatch scan finds
// that job and dispatches again — correct, and necessary, because the
// first publish may never have happened. But if it *did* happen, the same
// envelope is now on the stream twice and two workers pick it up.
//
// The DB's UNIQUE constraint (ADR-0007) makes the second one a no-op
// eventually, but only after a worker has already claimed it, so the guard
// is what stops the *duplicate delivery* rather than merely surviving it.
// Attempt is part of the key because a genuine retry — attempt 2 of the
// same work unit — must publish, and only a redelivery of the same attempt
// must not.
//
// The guard is an optimisation, not the guarantee. Redis losing the key
// (eviction, a flushed DB) degrades to exactly the pre-guard behaviour: a
// duplicate delivery that dedupe absorbs. Correctness never rests here.
func (c *Client) PublishEnvelope(ctx context.Context, env *codev1.StageEnvelope) (PublishResult, error) {
	stream, err := RequestStreamFor(env.GetStage())
	if err != nil {
		return PublishResult{}, err
	}

	if key := env.GetIdempotencyKey(); key != "" {
		fresh, err := c.claimPublish(ctx, stream, key, env.GetAttempt())
		if err != nil {
			return PublishResult{}, err
		}
		if !fresh {
			return PublishResult{Deduplicated: true}, nil
		}
	}

	values, err := envelopeFields(env)
	if err != nil {
		return PublishResult{}, err
	}
	id, err := c.xadd(ctx, stream, values)
	if err != nil {
		// Release the guard so the next dispatch scan can retry the
		// publish. Leaving it set would make a transient Redis write error
		// permanently suppress this attempt — the job would sit in a
		// *_queued state that nothing ever dispatches.
		c.releasePublish(ctx, stream, env.GetIdempotencyKey(), env.GetAttempt())
		return PublishResult{}, err
	}
	return PublishResult{MessageID: id}, nil
}

// PublishResultMessage XADDs a StageResult to stage.results. It exists for
// the orchestrator's own re-emission path and for tests standing in for a
// Python worker; in production the workers are this stream's producers.
func (c *Client) PublishResultMessage(ctx context.Context, res *codev1.StageResult) (string, error) {
	values, err := resultFields(res)
	if err != nil {
		return "", err
	}
	return c.xadd(ctx, StreamResults, values)
}

// PublishHeartbeat XADDs a StageHeartbeat to stage.progress. Same rationale
// as PublishResultMessage — workers are the real producers (§2.4).
func (c *Client) PublishHeartbeat(ctx context.Context, hb *codev1.StageHeartbeat) (string, error) {
	values, err := heartbeatFields(hb)
	if err != nil {
		return "", err
	}
	return c.xadd(ctx, StreamProgress, values)
}

// PublishJobSubmitted rings the doorbell for a newly created jobs row. It
// carries identifiers only — never a payload — because the durable record
// is the row itself (claude_context.md decision #34).
func (c *Client) PublishJobSubmitted(ctx context.Context, req JobRequest) (string, error) {
	return c.xadd(ctx, StreamJobSubmitted, map[string]any{
		FieldKind:          KindJobSubmitted,
		FieldJobID:         req.JobID.String(),
		"consultation_id":  req.ConsultationID.String(),
		"run_config_id":    req.RunConfigID.String(),
		FieldSchemaVersion: fmt.Sprint(SchemaVersion),
	})
}

func (c *Client) xadd(ctx context.Context, stream string, values map[string]any) (string, error) {
	id, err := c.rdb.XAdd(ctx, &redis.XAddArgs{
		Stream: stream,
		// Approximate trimming (`MAXLEN ~`) so Redis trims on whole-node
		// boundaries instead of walking the stream on every append.
		MaxLen: c.maxLen,
		Approx: true,
		Values: values,
	}).Result()
	if err != nil {
		return "", fmt.Errorf("queue: xadd to %s: %w", stream, err)
	}
	return id, nil
}

func publishGuardKey(stream, idempotencyKey string, attempt uint32) string {
	return fmt.Sprintf("coda:published:%s:%s:%d", stream, idempotencyKey, attempt)
}

// claimPublish reports whether this caller is the first to publish the
// triple. False means someone already did.
func (c *Client) claimPublish(ctx context.Context, stream, idempotencyKey string, attempt uint32) (bool, error) {
	ok, err := c.rdb.SetNX(ctx, publishGuardKey(stream, idempotencyKey, attempt), time.Now().UTC().Format(time.RFC3339Nano), c.publishDedupeTTL).Result()
	if err != nil {
		return false, fmt.Errorf("queue: publish guard for %s: %w", idempotencyKey, err)
	}
	return ok, nil
}

func (c *Client) releasePublish(ctx context.Context, stream, idempotencyKey string, attempt uint32) {
	if idempotencyKey == "" {
		return
	}
	// Best effort: if this DEL fails the guard expires on its own TTL.
	// Using a fresh context so a cancelled request context (the usual
	// reason the XADD failed) doesn't also block the cleanup.
	cleanupCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
	defer cancel()
	_ = c.rdb.Del(cleanupCtx, publishGuardKey(stream, idempotencyKey, attempt)).Err()
}
