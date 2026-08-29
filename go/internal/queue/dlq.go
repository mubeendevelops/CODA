package queue

import (
	"context"
	"fmt"

	"github.com/redis/go-redis/v9"
	"google.golang.org/protobuf/types/known/timestamppb"

	codev1 "coda/go/internal/genproto/coda/v1"
)

// DeadLetterRequest is everything needed to build a DeadLetter. The caller
// supplies the failure history because it lives in Postgres
// (job_stages.error_history), not in Redis — a DLQ message must remain
// reconstructible after the streams have been trimmed.
type DeadLetterRequest struct {
	Envelope    *codev1.StageEnvelope
	Attempts    []*codev1.AttemptError
	FinalStatus codev1.Status
}

// PublishDeadLetter routes a poison message to stage.dlq (§2.5).
//
// "Nothing is silently dropped" is the whole point of this function: the
// message carries the original envelope (so replay needs no other source),
// every attempt's error, the trace ID, and the terminal classification that
// sent it here. A DeadLetter with an empty Attempts list is a bug — it
// means a job was dead-lettered without anyone recording why.
//
// stage.dlq has no consumer group by design (§2.1: "manual / operator").
// Replay is an explicit operator action that re-XADDs with attempt = 1; it
// is deliberately not automated, because a DLQ that drains itself is
// indistinguishable from one that works.
func (c *Client) PublishDeadLetter(ctx context.Context, req DeadLetterRequest) (string, error) {
	if req.Envelope == nil {
		return "", fmt.Errorf("queue: dead letter requires the original envelope")
	}
	dl := &codev1.DeadLetter{
		JobId:            req.Envelope.GetJobId(),
		ConsultationId:   req.Envelope.GetConsultationId(),
		Stage:            req.Envelope.GetStage(),
		OriginalEnvelope: req.Envelope,
		Attempts:         req.Attempts,
		FinalStatus:      req.FinalStatus,
		DeadLetteredAt:   timestamppb.Now(),
	}
	values, err := deadLetterFields(dl)
	if err != nil {
		return "", err
	}
	return c.xadd(ctx, StreamDLQ, values)
}

// ReadDeadLetters reads the most recent DLQ entries without consuming them —
// XREVRANGE, not XREADGROUP, precisely because reading must not drain.
// Used by the DLQ-alerting periodic job and by operator tooling.
func (c *Client) ReadDeadLetters(ctx context.Context, count int64) ([]*codev1.DeadLetter, error) {
	entries, err := c.rdb.XRevRangeN(ctx, StreamDLQ, "+", "-", count).Result()
	if err != nil {
		if err == redis.Nil {
			return nil, nil
		}
		return nil, fmt.Errorf("queue: read %s: %w", StreamDLQ, err)
	}
	out := make([]*codev1.DeadLetter, 0, len(entries))
	for _, e := range entries {
		dl, err := DecodeDeadLetter(toMessage(StreamDLQ, "", e, false, 0))
		if err != nil {
			// One malformed entry must not blind the alerting job to the
			// rest of the queue.
			continue
		}
		out = append(out, dl)
	}
	return out, nil
}

// DLQDepth is the §8 metric "DLQ depth".
func (c *Client) DLQDepth(ctx context.Context) (int64, error) { return c.Len(ctx, StreamDLQ) }
