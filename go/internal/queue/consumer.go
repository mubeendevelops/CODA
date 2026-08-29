package queue

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/redis/go-redis/v9"
)

// Message is one consumed stream entry, decoupled from go-redis' XMessage
// so handlers and tests never import the Redis client.
type Message struct {
	ID     string
	Stream string
	Group  string
	Fields map[string]string
	// Reclaimed is true when this message came from XAUTOCLAIM rather than
	// a fresh XREADGROUP — i.e. a previous consumer took it and never
	// acknowledged it. Handlers use it to distinguish "new work" from
	// "work someone may have half-finished".
	Reclaimed bool
	// DeliveryCount is Redis' own redelivery counter for the entry, present
	// only on reclaimed messages.
	DeliveryCount int64
}

// Field reads one denormalised stream field.
func (m Message) Field(name string) string { return m.Fields[name] }

// Handler processes one message. Returning nil means "durably persisted —
// safe to acknowledge"; the consumer XACKs only then (§2.3). Returning an
// error leaves the entry in the PEL so XAUTOCLAIM can reclaim it, which is
// the recovery path a crash would have taken anyway.
//
// A handler must therefore never return nil on a path that skipped
// persistence: that is the one mistake this design cannot detect, and
// ADR-0007 names it explicitly ("getting XACK placement wrong silently
// loses work").
type Handler func(ctx context.Context, msg Message) error

// Consumer drains one consumer group, with XAUTOCLAIM-based recovery of
// entries a dead consumer left pending.
type Consumer struct {
	Client *Client
	Stream string
	Group  string
	// Name identifies this consumer within the group. It must be stable
	// across restarts of the same process (so its own PEL entries are
	// recognisable) and distinct across replicas.
	Name string
	// Block is how long XREADGROUP waits for new entries. Short enough that
	// shutdown is responsive, long enough not to spin.
	Block time.Duration
	// Batch is the max entries per read.
	Batch int64
	// MinIdle is how long an entry must sit unacknowledged before this
	// consumer reclaims it — the stage's visibility timeout (§4.2).
	MinIdle time.Duration
	// ReclaimEvery is the reaper cadence (§2.4: 30s).
	ReclaimEvery time.Duration
	Logger       *slog.Logger
}

func (c *Consumer) applyDefaults() {
	if c.Block <= 0 {
		c.Block = 5 * time.Second
	}
	if c.Batch <= 0 {
		c.Batch = 16
	}
	if c.MinIdle <= 0 {
		c.MinIdle = 5 * time.Minute
	}
	if c.ReclaimEvery <= 0 {
		c.ReclaimEvery = ReaperInterval
	}
	if c.Logger == nil {
		c.Logger = slog.Default()
	}
}

// Run consumes until ctx is cancelled. It interleaves fresh reads with
// periodic reclaim sweeps, so a consumer that restarts after a crash picks
// its own abandoned entries back up rather than waiting for another replica
// to notice.
func (c *Consumer) Run(ctx context.Context, h Handler) error {
	c.applyDefaults()
	if err := c.Client.EnsureGroup(ctx, c.Stream, c.Group); err != nil {
		return err
	}

	reclaim := time.NewTicker(c.ReclaimEvery)
	defer reclaim.Stop()

	// Reclaim once up front. A process that just restarted after a crash
	// has entries pending under its own consumer name that no timer should
	// make it wait 30 seconds to notice.
	if n, err := c.ReclaimOnce(ctx, h); err != nil {
		c.Logger.WarnContext(ctx, "queue: initial reclaim failed", "stream", c.Stream, "group", c.Group, "error", err)
	} else if n > 0 {
		c.Logger.InfoContext(ctx, "queue: reclaimed pending entries at startup", "stream", c.Stream, "group", c.Group, "count", n)
	}

	for {
		select {
		case <-ctx.Done():
			return nil
		case <-reclaim.C:
			if _, err := c.ReclaimOnce(ctx, h); err != nil && !errors.Is(err, context.Canceled) {
				c.Logger.WarnContext(ctx, "queue: reclaim sweep failed", "stream", c.Stream, "group", c.Group, "error", err)
			}
		default:
		}

		msgs, err := c.read(ctx)
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			c.Logger.ErrorContext(ctx, "queue: read failed", "stream", c.Stream, "group", c.Group, "error", err)
			// Back off briefly rather than hot-looping against a Redis
			// that is down; the reconnect is go-redis' job.
			select {
			case <-ctx.Done():
				return nil
			case <-time.After(time.Second):
			}
			continue
		}
		for _, msg := range msgs {
			c.handle(ctx, h, msg)
		}
	}
}

func (c *Consumer) read(ctx context.Context) ([]Message, error) {
	streams, err := c.Client.rdb.XReadGroup(ctx, &redis.XReadGroupArgs{
		Group:    c.Group,
		Consumer: c.Name,
		Streams:  []string{c.Stream, ">"},
		Count:    c.Batch,
		Block:    c.Block,
	}).Result()
	if err != nil {
		// redis.Nil is the normal "block elapsed with nothing new".
		if errors.Is(err, redis.Nil) {
			return nil, nil
		}
		return nil, fmt.Errorf("queue: xreadgroup %s/%s: %w", c.Stream, c.Group, err)
	}
	var out []Message
	for _, s := range streams {
		for _, m := range s.Messages {
			out = append(out, toMessage(s.Stream, c.Group, m, false, 0))
		}
	}
	return out, nil
}

// ReclaimOnce runs a single XAUTOCLAIM sweep, handing every reclaimed entry
// to h. Returns how many entries were reclaimed.
//
// This is §2.4's recovery primitive. An entry sitting in the PEL longer
// than MinIdle means the consumer that read it neither acknowledged nor
// died gracefully — a killed worker, an OOM, a lost network partition.
// Without this the entry is pending forever and that consultation stops
// dead with no error anywhere.
func (c *Consumer) ReclaimOnce(ctx context.Context, h Handler) (int, error) {
	c.applyDefaults()
	total := 0
	start := "0-0"
	for {
		msgs, next, err := c.Client.Reclaim(ctx, ReclaimArgs{
			Stream:   c.Stream,
			Group:    c.Group,
			Consumer: c.Name,
			MinIdle:  c.MinIdle,
			Count:    c.Batch,
			Start:    start,
		})
		if err != nil {
			return total, err
		}
		for _, msg := range msgs {
			c.handle(ctx, h, msg)
			total++
		}
		// XAUTOCLAIM returns "0-0" as the next cursor when it has walked
		// the whole PEL. Stopping on a short batch instead would leave
		// entries unreclaimed whenever the PEL is sparse.
		if next == "0-0" || next == "" || len(msgs) == 0 {
			return total, nil
		}
		start = next
	}
}

func (c *Consumer) handle(ctx context.Context, h Handler, msg Message) {
	if err := h(ctx, msg); err != nil {
		// Deliberately not acknowledged: the entry stays in the PEL and a
		// later reclaim sweep retries it. This is the only correct
		// response to "persistence failed" — acking here would drop it.
		c.Logger.ErrorContext(ctx, "queue: handler failed, leaving entry pending for reclaim",
			"stream", msg.Stream, "group", msg.Group, "message_id", msg.ID,
			"job_id", msg.Field(FieldJobID), "stage", msg.Field(FieldStage), "error", err)
		return
	}
	if err := c.Client.Ack(ctx, msg.Stream, msg.Group, msg.ID); err != nil {
		// The work is durably persisted; only the ack failed. The entry
		// will be reclaimed and re-handled, and the dedupe check absorbs
		// it (§2.3's "one redundant no-op delivery — acceptable").
		c.Logger.WarnContext(ctx, "queue: ack failed after successful handling",
			"stream", msg.Stream, "message_id", msg.ID, "error", err)
	}
}

func toMessage(stream, group string, m redis.XMessage, reclaimed bool, deliveries int64) Message {
	f := make(map[string]string, len(m.Values))
	for k, v := range m.Values {
		if s, ok := v.(string); ok {
			f[k] = s
			continue
		}
		f[k] = fmt.Sprint(v)
	}
	return Message{ID: m.ID, Stream: stream, Group: group, Fields: f, Reclaimed: reclaimed, DeliveryCount: deliveries}
}
