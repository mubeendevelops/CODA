package auth

import (
	"encoding/json"
	"errors"
	"net/http"
	"strings"

	"github.com/google/uuid"
)

// Authenticate returns middleware that requires a valid `Authorization:
// Bearer <token>` access token, verifies it against svc, and stashes the
// resulting Claims in the request context (auth.ClaimsFromContext).
// Missing, malformed, expired, or forged tokens all fail closed with 401 —
// there is no anonymous fallthrough for routes mounted behind this
// middleware.
func Authenticate(svc *JWTService) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			header := r.Header.Get("Authorization")
			token, ok := bearerToken(header)
			if !ok {
				writeAuthError(w, http.StatusUnauthorized, "missing or malformed Authorization header")
				return
			}

			claims, err := svc.ParseAccessToken(token)
			if err != nil {
				status := http.StatusUnauthorized
				msg := "invalid token"
				if errors.Is(err, ErrExpiredToken) {
					msg = "token expired"
				}
				writeAuthError(w, status, msg)
				return
			}

			r = r.WithContext(WithClaims(r.Context(), claims))
			next.ServeHTTP(w, r)
		})
	}
}

// RequireRole returns middleware that rejects requests whose authenticated
// role is not in allowed, with 403. It must run after Authenticate — a
// missing claims value (Authenticate not mounted, or bypassed) is treated
// as a server misconfiguration and fails closed with 401 rather than
// panicking or, worse, allowing the request through.
//
// This is deliberately also re-checked at the handler level for any
// operation with a per-resource nuance (e.g. "doctor may act only on their
// own-org data") — RequireRole enforces the coarse "may this role ever call
// this route" check; handlers enforce the fine-grained one.
func RequireRole(allowed ...Role) func(http.Handler) http.Handler {
	allowedSet := make(map[Role]bool, len(allowed))
	for _, r := range allowed {
		allowedSet[r] = true
	}
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			claims, ok := ClaimsFromContext(r.Context())
			if !ok {
				writeAuthError(w, http.StatusUnauthorized, "authentication required")
				return
			}
			if !allowedSet[claims.Role] {
				writeAuthError(w, http.StatusForbidden, "role not permitted for this operation")
				return
			}
			next.ServeHTTP(w, r)
		})
	}
}

// RequireSameOrg is the handler-level org-scoping check
// (docs/architecture.md §7.6: "no cross-org read is expressible through
// the API"). Handlers call this after loading a resource, comparing the
// caller's claims against the resource's org_id, rather than relying on
// route/middleware structure alone to enforce tenancy. Returns false (and
// has already written the response) when the caller must not proceed.
//
// Cross-org access is reported as 404, not 403: revealing that a resource
// exists in another org is itself a (minor) information leak, and 404 is
// indistinguishable from "no such resource" — the standard mitigation.
func RequireSameOrg(w http.ResponseWriter, claims *Claims, resourceOrgID uuid.UUID) bool {
	if claims.OrgID != resourceOrgID {
		writeAuthError(w, http.StatusNotFound, "not found")
		return false
	}
	return true
}

func bearerToken(header string) (string, bool) {
	const prefix = "Bearer "
	if !strings.HasPrefix(header, prefix) {
		return "", false
	}
	token := strings.TrimSpace(strings.TrimPrefix(header, prefix))
	if token == "" {
		return "", false
	}
	return token, true
}

func writeAuthError(w http.ResponseWriter, status int, message string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]string{"error": message})
}
