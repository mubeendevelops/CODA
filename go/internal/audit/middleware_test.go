package audit

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/go-chi/chi/v5"
	"github.com/google/uuid"

	"coda/go/internal/auth"
)

type fakeRecorder struct {
	calls []Entry
	err   error
}

func (f *fakeRecorder) Record(_ context.Context, e Entry) error {
	f.calls = append(f.calls, e)
	return f.err
}

func discardLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func TestMiddleware_MutatingRequestIsRecorded(t *testing.T) {
	rec := &fakeRecorder{}
	claims := &auth.Claims{UserID: uuid.New(), OrgID: uuid.New(), Role: auth.RoleAdmin}

	router := chi.NewRouter()
	router.Use(Middleware(rec, discardLogger()))
	router.Post("/v1/users", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusCreated)
	})

	req := httptest.NewRequest(http.MethodPost, "/v1/users", nil)
	req = req.WithContext(auth.WithClaims(req.Context(), claims))
	w := httptest.NewRecorder()

	router.ServeHTTP(w, req)

	if len(rec.calls) != 1 {
		t.Fatalf("expected 1 recorded entry, got %d", len(rec.calls))
	}
	entry := rec.calls[0]
	if entry.OrgID != claims.OrgID {
		t.Errorf("OrgID = %v, want %v", entry.OrgID, claims.OrgID)
	}
	if entry.ActorUserID == nil || *entry.ActorUserID != claims.UserID {
		t.Errorf("ActorUserID = %v, want %v", entry.ActorUserID, claims.UserID)
	}
	if entry.Outcome != OutcomeSuccess {
		t.Errorf("Outcome = %v, want %v", entry.Outcome, OutcomeSuccess)
	}
	if entry.Action != "POST /v1/users" {
		t.Errorf("Action = %q, want %q", entry.Action, "POST /v1/users")
	}
}

func TestMiddleware_PlainGETIsNotRecorded(t *testing.T) {
	rec := &fakeRecorder{}
	claims := &auth.Claims{UserID: uuid.New(), OrgID: uuid.New(), Role: auth.RoleDoctor}

	router := chi.NewRouter()
	router.Use(Middleware(rec, discardLogger()))
	router.Get("/v1/users", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	})

	req := httptest.NewRequest(http.MethodGet, "/v1/users", nil)
	req = req.WithContext(auth.WithClaims(req.Context(), claims))
	w := httptest.NewRecorder()

	router.ServeHTTP(w, req)

	if len(rec.calls) != 0 {
		t.Fatalf("expected GET requests not marked clinical to go unrecorded, got %d entries", len(rec.calls))
	}
}

func TestMiddleware_ClinicalReadGETIsRecorded(t *testing.T) {
	rec := &fakeRecorder{}
	claims := &auth.Claims{UserID: uuid.New(), OrgID: uuid.New(), Role: auth.RoleDoctor}
	consultationID := uuid.New()

	router := chi.NewRouter()
	router.Use(Middleware(rec, discardLogger()))
	router.Get("/v1/consultations/{id}", func(w http.ResponseWriter, r *http.Request) {
		SetResource(r.Context(), "consultation", consultationID.String())
		SetAction(r.Context(), "consultation.read")
		MarkClinicalRead(r.Context())
		w.WriteHeader(http.StatusOK)
	})

	req := httptest.NewRequest(http.MethodGet, "/v1/consultations/"+consultationID.String(), nil)
	req = req.WithContext(auth.WithClaims(req.Context(), claims))
	w := httptest.NewRecorder()

	router.ServeHTTP(w, req)

	if len(rec.calls) != 1 {
		t.Fatalf("expected 1 recorded entry for a marked clinical read, got %d", len(rec.calls))
	}
	entry := rec.calls[0]
	if entry.ResourceType != "consultation" || entry.ResourceID != consultationID.String() {
		t.Errorf("resource = %s/%s, want consultation/%s", entry.ResourceType, entry.ResourceID, consultationID)
	}
	if entry.Action != "consultation.read" {
		t.Errorf("Action = %q, want %q", entry.Action, "consultation.read")
	}
}

func TestMiddleware_FailureStatusRecordsFailureOutcome(t *testing.T) {
	rec := &fakeRecorder{}
	claims := &auth.Claims{UserID: uuid.New(), OrgID: uuid.New(), Role: auth.RoleReviewer}

	router := chi.NewRouter()
	router.Use(Middleware(rec, discardLogger()))
	router.Delete("/v1/thing/{id}", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusForbidden)
	})

	req := httptest.NewRequest(http.MethodDelete, "/v1/thing/1", nil)
	req = req.WithContext(auth.WithClaims(req.Context(), claims))
	w := httptest.NewRecorder()

	router.ServeHTTP(w, req)

	if len(rec.calls) != 1 {
		t.Fatalf("expected 1 recorded entry, got %d", len(rec.calls))
	}
	if rec.calls[0].Outcome != OutcomeFailure {
		t.Errorf("Outcome = %v, want %v for a 403 response", rec.calls[0].Outcome, OutcomeFailure)
	}
}

func TestMiddleware_NoClaimsIsSkippedNotPanicked(t *testing.T) {
	rec := &fakeRecorder{}

	router := chi.NewRouter()
	router.Use(Middleware(rec, discardLogger()))
	router.Post("/v1/orphan", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	})

	req := httptest.NewRequest(http.MethodPost, "/v1/orphan", nil)
	w := httptest.NewRecorder()

	router.ServeHTTP(w, req) // must not panic despite no auth.Claims in context

	if len(rec.calls) != 0 {
		t.Fatalf("expected no entry recorded without claims, got %d", len(rec.calls))
	}
	if w.Code != http.StatusOK {
		t.Errorf("handler response status = %d, want %d (audit gap must not break the request)", w.Code, http.StatusOK)
	}
}

func TestMiddleware_RecorderErrorDoesNotAlterResponse(t *testing.T) {
	rec := &fakeRecorder{err: errors.New("db unavailable")}
	claims := &auth.Claims{UserID: uuid.New(), OrgID: uuid.New(), Role: auth.RoleAdmin}

	router := chi.NewRouter()
	router.Use(Middleware(rec, discardLogger()))
	router.Post("/v1/users", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusCreated)
	})

	req := httptest.NewRequest(http.MethodPost, "/v1/users", nil)
	req = req.WithContext(auth.WithClaims(req.Context(), claims))
	w := httptest.NewRecorder()

	router.ServeHTTP(w, req)

	if w.Code != http.StatusCreated {
		t.Errorf("status = %d, want %d — a failing audit write must not change the client-visible response", w.Code, http.StatusCreated)
	}
}
