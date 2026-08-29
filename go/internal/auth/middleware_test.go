package auth

import (
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/google/uuid"
)

func TestAuthenticate(t *testing.T) {
	svc := NewJWTService("test-signing-key-at-least-16-bytes", time.Hour)
	userID, orgID := uuid.New(), uuid.New()
	validToken, err := svc.IssueAccessToken(userID, orgID, RoleDoctor)
	if err != nil {
		t.Fatalf("IssueAccessToken: %v", err)
	}

	tests := []struct {
		name       string
		authHeader string
		wantStatus int
	}{
		{"valid bearer token", "Bearer " + validToken, http.StatusOK},
		{"missing header", "", http.StatusUnauthorized},
		{"malformed - no Bearer prefix", validToken, http.StatusUnauthorized},
		{"malformed - empty token after prefix", "Bearer ", http.StatusUnauthorized},
		{"invalid token", "Bearer garbage", http.StatusUnauthorized},
		{"wrong scheme", "Basic dXNlcjpwYXNz", http.StatusUnauthorized},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			var reachedHandler bool
			next := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				reachedHandler = true
				claims, ok := ClaimsFromContext(r.Context())
				if !ok || claims.UserID != userID {
					t.Error("expected claims in context matching issued token")
				}
				w.WriteHeader(http.StatusOK)
			})

			req := httptest.NewRequest(http.MethodGet, "/protected", nil)
			if tt.authHeader != "" {
				req.Header.Set("Authorization", tt.authHeader)
			}
			rec := httptest.NewRecorder()

			Authenticate(svc)(next).ServeHTTP(rec, req)

			if rec.Code != tt.wantStatus {
				t.Errorf("status = %d, want %d", rec.Code, tt.wantStatus)
			}
			if tt.wantStatus == http.StatusOK && !reachedHandler {
				t.Error("expected handler to be reached")
			}
			if tt.wantStatus != http.StatusOK && reachedHandler {
				t.Error("handler must not run when authentication fails")
			}
		})
	}
}

func TestRequireRole(t *testing.T) {
	tests := []struct {
		name       string
		callerRole Role
		allowed    []Role
		noClaims   bool
		wantStatus int
	}{
		{"admin allowed for admin-only route", RoleAdmin, []Role{RoleAdmin}, false, http.StatusOK},
		{"doctor denied on admin-only route", RoleDoctor, []Role{RoleAdmin}, false, http.StatusForbidden},
		{"reviewer allowed when in the allowed set", RoleReviewer, []Role{RoleAdmin, RoleDoctor, RoleReviewer}, false, http.StatusOK},
		{"auditor denied from clinical-data route", RoleAuditor, []Role{RoleAdmin, RoleDoctor, RoleReviewer}, false, http.StatusForbidden},
		{"no claims in context at all", RoleAdmin, []Role{RoleAdmin}, true, http.StatusUnauthorized},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			next := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				w.WriteHeader(http.StatusOK)
			})

			req := httptest.NewRequest(http.MethodGet, "/resource", nil)
			if !tt.noClaims {
				claims := &Claims{UserID: uuid.New(), OrgID: uuid.New(), Role: tt.callerRole}
				req = req.WithContext(WithClaims(req.Context(), claims))
			}
			rec := httptest.NewRecorder()

			RequireRole(tt.allowed...)(next).ServeHTTP(rec, req)

			if rec.Code != tt.wantStatus {
				t.Errorf("status = %d, want %d", rec.Code, tt.wantStatus)
			}
		})
	}
}

func TestRequireSameOrg(t *testing.T) {
	ownOrg := uuid.New()
	otherOrg := uuid.New()
	claims := &Claims{UserID: uuid.New(), OrgID: ownOrg, Role: RoleDoctor}

	tests := []struct {
		name          string
		resourceOrgID uuid.UUID
		wantOK        bool
		wantStatus    int
	}{
		{"same org allowed", ownOrg, true, 0},
		{"different org rejected as not found", otherOrg, false, http.StatusNotFound},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			rec := httptest.NewRecorder()
			ok := RequireSameOrg(rec, claims, tt.resourceOrgID)
			if ok != tt.wantOK {
				t.Errorf("RequireSameOrg() = %v, want %v", ok, tt.wantOK)
			}
			if !tt.wantOK && rec.Code != tt.wantStatus {
				t.Errorf("status = %d, want %d", rec.Code, tt.wantStatus)
			}
			// Cross-org must never be distinguishable from "does not
			// exist" — asserting 404 specifically (not 403) is the point
			// of this test, not incidental.
			if !tt.wantOK && rec.Code == http.StatusForbidden {
				t.Error("cross-org access must not leak as 403 (reveals resource exists elsewhere)")
			}
		})
	}
}
