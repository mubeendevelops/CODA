package queue

import (
	"context"
	"fmt"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
)

// ReclaimArgs parameterises one XAUTOCLAIM call.
type ReclaimArgs struct {
	Stream string
	Group  string
	// Consumer takes ownership of the reclaimed entries.
	Consumer string
	// MinIdle is the visibility timeout: only entries idle at least this
	// long are reclaimed (§2.4). Set it below a stage's real runtime and
	// the reaper will steal messages from workers that are merely slow,
	// duplicating expensive work — which is why §4.2 sizes it at ~2x the
	// observed p95 rather than at the soft deadline.
	MinIdle time.Duration
	Count   int64
	// Start is the PEL cursor; "0-0" begins a sweep.
	Start string
}

// Reclaim issues one XAUTOCLAIM and returns the reclaimed entries plus the
// cursor for the next call ("0-0" when the PEL has been fully walked).
//
// Reclaimed entries keep their original fields, so trace_id and
// idempotency_key survive the handover — §2.4's requirement, and the reason
// a partially-complete stage that already persisted its result is absorbed
// by the dedupe check instead of being recomputed.
func (c *Client) Reclaim(ctx context.Context, args ReclaimArgs) ([]Message, string, error) {
	if args.Count <= 0 {
		args.Count = 16
	}
	if args.Start == "" {
		args.Start = "0-0"
	}
	msgs, next, err := c.rdb.XAutoClaim(ctx, &redis.XAutoClaimArgs{
		Stream:   args.Stream,
		Group:    args.Group,
		Consumer: args.Consumer,
		MinIdle:  args.MinIdle,
		Start:    args.Start,
		Count:    args.Count,
	}).Result()
	if err != nil {
		// A group that does not exist yet has nothing to reclaim. This is
		// normal on a cold system and must not fail a reaper tick.
		if strings.Contains(err.Error(), "NOGROUP") {
			return nil, "0-0", nil
		}
		return nil, "", fmt.Errorf("queue: xautoclaim %s/%s: %w", args.Stream, args.Group, err)
	}

	out := make([]Message, 0, len(msgs))
	for _, m := range msgs {
		// XAUTOCLAIM yields entries whose data was trimmed away as
		// ID-only messages with no fields; Redis deletes them from the PEL
		// itself, so there is nothing left to hand a handler.
		if len(m.Values) == 0 {
			continue
		}
		out = append(out, toMessage(args.Stream, args.Group, m, true, 0))
	}
	return out, next, nil
}

// ReclaimStale reclaims entries from *another* consumer group — the pattern
// the orchestrator's reaper uses against stage.asr / stage.nlp (§2.4: "it
// issues XAUTOCLAIM against each stream for entries idle longer than the
// stage's visibility timeout, then re-dispatches with attempt + 1").
//
// The orchestrator is the producer of those streams, not a member of the
// workers' group, so it joins the group only to claim what a dead worker
// abandoned. Having claimed an entry it must also acknowledge it: the
// re-dispatch publishes a *fresh* envelope at attempt+1, so leaving the old
// entry pending would move the leak from the dead worker's PEL into the
// orchestrator's own.
func (c *Client) ReclaimStale(ctx context.Context, stream, group, consumer string, minIdle time.Duration, count int64) ([]Message, error) {
	var all []Message
	start := "0-0"
	for {
		msgs, next, err := c.Reclaim(ctx, ReclaimArgs{
			Stream: stream, Group: group, Consumer: consumer,
			MinIdle: minIdle, Count: count, Start: start,
		})
		if err != nil {
			return all, err
		}
		all = append(all, msgs...)
		if next == "0-0" || next == "" || len(msgs) == 0 {
			return all, nil
		}
		start = next
	}
}
