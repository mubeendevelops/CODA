package queue

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"

	"coda/go/internal/config"
)

// Client is the Redis Streams transport (ADR-0002). It is the only place in
// the Go codebase that speaks Redis stream commands; the orchestrator's
// state machine talks to this, never to go-redis.
type Client struct {
	rdb redis.UniversalClient

	// publishDedupeTTL bounds how long a published (stream, key, attempt)
	// triple is remembered for producer-side dedupe. See Publish.
	publishDedupeTTL time.Duration

	// maxLen bounds each stream with XADD MAXLEN ~. Streams are a control
	// channel, not durable storage (§1.2: "redis must NOT hold anything
	// that must survive a restart") — Postgres holds the durable record, so
	// trimming an old entry loses nothing. Unbounded streams are how a
	// long-running eval sweep runs Redis out of memory.
	maxLen int64
}

// Options tune a Client. The zero value is the production default.
type Options struct {
	// PublishDedupeTTL defaults to 24h — comfortably longer than the
	// longest stage window in §4.2 (the 90-minute GoT visibility timeout)
	// and than any plausible retry chain, so the guard cannot expire while
	// the work it guards is still in flight.
	PublishDedupeTTL time.Duration
	// MaxStreamLen defaults to 10000 entries per stream.
	MaxStreamLen int64
}

// NewClient dials Redis and verifies connectivity before returning, the
// same fail-at-startup posture as db.NewPool and storage.NewClient.
func NewClient(ctx context.Context, cfg config.Redis, opts Options) (*Client, error) {
	rdb := redis.NewClient(&redis.Options{
		Addr:     cfg.Addr,
		Password: cfg.Password,
		DB:       cfg.DB,
	})
	if err := rdb.Ping(ctx).Err(); err != nil {
		_ = rdb.Close()
		return nil, fmt.Errorf("queue: ping redis at %s: %w", cfg.Addr, err)
	}
	return NewClientFromRedis(rdb, opts), nil
}

// NewClientFromRedis wraps an already-constructed Redis client. Tests use it
// to point at a testcontainer without going through config.
func NewClientFromRedis(rdb redis.UniversalClient, opts Options) *Client {
	ttl := opts.PublishDedupeTTL
	if ttl <= 0 {
		ttl = 24 * time.Hour
	}
	maxLen := opts.MaxStreamLen
	if maxLen <= 0 {
		maxLen = 10000
	}
	return &Client{rdb: rdb, publishDedupeTTL: ttl, maxLen: maxLen}
}

// Redis exposes the underlying client for the narrow set of callers that
// legitimately need it (Asynq inspection, test fixtures). Pipeline code must
// not use it — that would put stream commands outside this package.
func (c *Client) Redis() redis.UniversalClient { return c.rdb }

// Close releases the connection pool.
func (c *Client) Close() error { return c.rdb.Close() }

// EnsureGroups creates every stream and consumer group this system uses, if
// they do not already exist.
//
// MKSTREAM matters: without it, XGROUP CREATE against a stream nothing has
// XADDed to yet fails, which means the orchestrator could only start
// *after* the first message — a startup ordering dependency between
// services that would surface as a mysterious cold-start failure. Called at
// orchestrator boot, and idempotent, so a restart is a no-op.
func (c *Client) EnsureGroups(ctx context.Context) error {
	for _, s := range []struct{ stream, group string }{
		{StreamASR, GroupASRWorkers},
		{StreamNLP, GroupNLPWorkers},
		{StreamResults, GroupOrchestrator},
		{StreamProgress, GroupOrchestrator},
		{StreamJobSubmitted, GroupOrchestrator},
	} {
		if err := c.EnsureGroup(ctx, s.stream, s.group); err != nil {
			return err
		}
	}
	// stage.dlq has no consumer group by design (§2.1: "manual /
	// operator"). It still needs to exist so XLEN-based alerting has
	// something to read on a system that has never dead-lettered anything.
	if err := c.ensureStream(ctx, StreamDLQ); err != nil {
		return err
	}
	return nil
}

// EnsureGroup creates one consumer group at the stream's tail, creating the
// stream if needed. "$" rather than "0" is deliberate: a newly created
// group starts from *now*, so standing up a new consumer does not replay
// the entire history of already-processed messages.
func (c *Client) EnsureGroup(ctx context.Context, stream, group string) error {
	err := c.rdb.XGroupCreateMkStream(ctx, stream, group, "$").Err()
	if err != nil && !isBusyGroup(err) {
		return fmt.Errorf("queue: create group %s on %s: %w", group, stream, err)
	}
	return nil
}

func (c *Client) ensureStream(ctx context.Context, stream string) error {
	// XGroupCreateMkStream is the only MKSTREAM-capable command go-redis
	// exposes; creating and immediately destroying a throwaway group is the
	// standard way to materialise an empty stream without XADDing a
	// sentinel entry that a consumer would then have to know to ignore.
	const bootstrapGroup = "__bootstrap__"
	if err := c.rdb.XGroupCreateMkStream(ctx, stream, bootstrapGroup, "$").Err(); err != nil && !isBusyGroup(err) {
		return fmt.Errorf("queue: create stream %s: %w", stream, err)
	}
	if err := c.rdb.XGroupDestroy(ctx, stream, bootstrapGroup).Err(); err != nil {
		return fmt.Errorf("queue: drop bootstrap group on %s: %w", stream, err)
	}
	return nil
}

func isBusyGroup(err error) bool {
	return err != nil && strings.HasPrefix(err.Error(), "BUSYGROUP")
}

// Ack acknowledges one message. Callers must invoke it only after the
// result is durably persisted (§2.3's acknowledgement rule) — acking on
// receipt would turn at-least-once into at-most-once and silently drop a
// consultation on any crash.
func (c *Client) Ack(ctx context.Context, stream, group string, ids ...string) error {
	if len(ids) == 0 {
		return nil
	}
	if err := c.rdb.XAck(ctx, stream, group, ids...).Err(); err != nil {
		return fmt.Errorf("queue: ack %v on %s/%s: %w", ids, stream, group, err)
	}
	return nil
}

// Len reports a stream's entry count. Used for DLQ-depth alerting (§8).
func (c *Client) Len(ctx context.Context, stream string) (int64, error) {
	n, err := c.rdb.XLen(ctx, stream).Result()
	if err != nil && !errors.Is(err, redis.Nil) {
		return 0, fmt.Errorf("queue: xlen %s: %w", stream, err)
	}
	return n, nil
}

// PendingCount reports how many entries sit unacknowledged in a group's
// PEL — the signal that workers are dying mid-stage rather than merely
// running slowly.
func (c *Client) PendingCount(ctx context.Context, stream, group string) (int64, error) {
	res, err := c.rdb.XPending(ctx, stream, group).Result()
	if err != nil {
		if errors.Is(err, redis.Nil) {
			return 0, nil
		}
		// A group that has never existed is not an error worth failing a
		// metrics scrape over.
		if strings.Contains(err.Error(), "NOGROUP") {
			return 0, nil
		}
		return 0, fmt.Errorf("queue: xpending %s/%s: %w", stream, group, err)
	}
	return res.Count, nil
}
