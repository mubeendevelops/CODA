package audit

import (
	"context"
	"log/slog"
	"net"
	"net/http"
	"net/netip"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"

	"coda/go/internal/auth"
)

type contextKey int

const stateContextKey contextKey = iota

// state is a mutable, per-request scratchpad a handler can annotate before
// Middleware writes the entry after the handler returns — mirrors chi's own
// RouteContext pattern (a pointer stashed in context, mutated in place)
// rather than requiring every handler to thread a new context back out.
type state struct {
	resourceType string
	resourceID   string
	action       string
	before       []byte
	after        []byte
	clinicalRead bool
}

// SetResource lets a handler record which resource it acted on — the URL
// alone (e.g. /v1/consultations/{id}) only gives the ID, not a human
// resource_type label, and a handler is the only place that knows both.
func SetResource(ctx context.Context, resourceType, resourceID string) {
	if s, ok := ctx.Value(stateContextKey).(*state); ok {
		s.resourceType = resourceType
		s.resourceID = resourceID
	}
}

// SetAction overrides the default "METHOD /route/pattern" action label with
// a more semantic one (e.g. "consultation.read").
func SetAction(ctx context.Context, action string) {
	if s, ok := ctx.Value(stateContextKey).(*state); ok {
		s.action = action
	}
}

// SetBefore attaches a pre-mutation snapshot to the audit entry — for a
// handler whose action deletes or overwrites the resource (DPDP erasure,
// hard delete), this is the only record of what existed once the request
// completes, since the row itself may be gone by the time Middleware writes
// the entry.
func SetBefore(ctx context.Context, before []byte) {
	if s, ok := ctx.Value(stateContextKey).(*state); ok {
		s.before = before
	}
}

// SetAfter attaches a post-mutation snapshot to the audit entry.
func SetAfter(ctx context.Context, after []byte) {
	if s, ok := ctx.Value(stateContextKey).(*state); ok {
		s.after = after
	}
}

// MarkClinicalRead flags the current request as a read of clinical data, so
// Middleware logs it even though GET requests are not audited by default
// (only mutations are, unless flagged). Call this from any handler that
// returns consultation content, transcripts, or clinical notes.
func MarkClinicalRead(ctx context.Context) {
	if s, ok := ctx.Value(stateContextKey).(*state); ok {
		s.clinicalRead = true
	}
}

// Middleware records one audit_log entry for every mutating request
// (POST/PUT/PATCH/DELETE) and every request a handler marks via
// MarkClinicalRead, capturing actor, action, resource, org, IP, and
// outcome (docs/architecture.md §7.4).
//
// It must be mounted after auth.Authenticate: the actor and org come from
// the request's auth.Claims, and a request with no claims in context (an
// unauthenticated route reachable through this middleware by mistake) is
// skipped with a warning log rather than failing the request — audit
// logging must never be able to break the API it's observing.
func Middleware(recorder Recorder, logger *slog.Logger) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			s := &state{}
			ctx := context.WithValue(r.Context(), stateContextKey, s)
			r = r.WithContext(ctx)

			ww := middleware.NewWrapResponseWriter(w, r.ProtoMajor)
			next.ServeHTTP(ww, r)

			mutating := isMutatingMethod(r.Method)
			if !mutating && !s.clinicalRead {
				return
			}

			claims, ok := auth.ClaimsFromContext(r.Context())
			if !ok {
				logger.WarnContext(r.Context(), "audit: no authenticated claims in context, skipping entry",
					"method", r.Method, "path", r.URL.Path)
				return
			}

			resourceType := s.resourceType
			if resourceType == "" {
				resourceType = chi.RouteContext(r.Context()).RoutePattern()
			}
			action := s.action
			if action == "" {
				action = r.Method + " " + chi.RouteContext(r.Context()).RoutePattern()
			}

			status := ww.Status()
			if status == 0 {
				status = http.StatusOK
			}
			outcome := OutcomeSuccess
			if status >= 400 {
				outcome = OutcomeFailure
			}

			userID := claims.UserID
			entry := Entry{
				OrgID:        claims.OrgID,
				ActorUserID:  &userID,
				Action:       action,
				ResourceType: resourceType,
				ResourceID:   s.resourceID,
				Before:       s.before,
				After:        s.after,
				TraceID:      middleware.GetReqID(r.Context()),
				IP:           parseIP(r.RemoteAddr),
				Outcome:      outcome,
			}

			if err := recorder.Record(r.Context(), entry); err != nil {
				logger.ErrorContext(r.Context(), "audit: failed to record entry", "error", err,
					"action", action, "resource_type", resourceType)
			}
		})
	}
}

func isMutatingMethod(method string) bool {
	switch method {
	case http.MethodPost, http.MethodPut, http.MethodPatch, http.MethodDelete:
		return true
	default:
		return false
	}
}

// parseIP tolerates RemoteAddr being host:port, a bare host (as tests often
// set it), or unparseable — audit logging must degrade, never fail the
// request over a malformed address.
func parseIP(remoteAddr string) *netip.Addr {
	host := remoteAddr
	if h, _, err := net.SplitHostPort(remoteAddr); err == nil {
		host = h
	}
	addr, err := netip.ParseAddr(host)
	if err != nil {
		return nil
	}
	return &addr
}
