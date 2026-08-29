// Package audit owns audit_log writes: actor, action, resource, org, IP,
// and outcome for every mutating request and every read of clinical data
// (docs/architecture.md §7.4, claude_context.md task instructions).
package audit

import (
	"context"
	"net/netip"

	"github.com/google/uuid"

	"coda/go/internal/db/sqlc"
)

type Outcome string

const (
	OutcomeSuccess Outcome = "success"
	OutcomeFailure Outcome = "failure"
)

// Entry is one audit_log row, independent of sqlc's generated params type so
// callers (handlers, middleware, tests) don't need to import the db layer
// just to construct one.
type Entry struct {
	OrgID        uuid.UUID
	ActorUserID  *uuid.UUID
	ActorService string
	Action       string
	ResourceType string
	ResourceID   string
	Before       []byte
	After        []byte
	TraceID      string
	IP           *netip.Addr
	Outcome      Outcome
}

// Recorder writes one Entry. It is an interface — not a concrete *sqlc.Queries
// dependency — so unit tests can inject a fake and assert on what would have
// been written without a database (docs/architecture.md's testability
// posture; see middleware_test.go).
type Recorder interface {
	Record(ctx context.Context, e Entry) error
}

// auditLogWriter is the minimal slice of *sqlc.Queries the SQL recorder
// needs, so tests can also satisfy it with a narrower fake than the full
// Queries interface if they want to exercise SQLRecorder itself.
type auditLogWriter interface {
	CreateAuditLogEntry(ctx context.Context, arg sqlc.CreateAuditLogEntryParams) (*sqlc.AuditLog, error)
}

// SQLRecorder is the production Recorder: every entry is a real INSERT into
// the append-only audit_log table.
type SQLRecorder struct {
	q auditLogWriter
}

func NewSQLRecorder(q *sqlc.Queries) *SQLRecorder {
	return &SQLRecorder{q: q}
}

func (r *SQLRecorder) Record(ctx context.Context, e Entry) error {
	params := sqlc.CreateAuditLogEntryParams{
		OrgID:        e.OrgID,
		ActorService: pgText(e.ActorService),
		Action:       e.Action,
		ResourceType: e.ResourceType,
		ResourceID:   e.ResourceID,
		Before:       e.Before,
		After:        e.After,
		TraceID:      e.TraceID,
		Ip:           e.IP,
		Outcome:      string(e.Outcome),
	}
	if e.ActorUserID != nil {
		params.ActorUserID = pgUUID(*e.ActorUserID)
	}
	_, err := r.q.CreateAuditLogEntry(ctx, params)
	return err
}
