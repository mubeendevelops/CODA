package http

import (
	"net/http"

	"coda/go/internal/auth"
)

// ListUsers returns every user in the caller's own org. Admin-only (route
// middleware); org-scoping is structural here — the query only ever takes
// claims.OrgID, so there is no parameter an attacker could substitute to
// read another org's users.
func (h *Handlers) ListUsers(w http.ResponseWriter, r *http.Request) {
	claims, ok := auth.ClaimsFromContext(r.Context())
	if !ok {
		writeError(w, http.StatusUnauthorized, "authentication required")
		return
	}

	users, err := h.queries.ListUsersByOrg(r.Context(), claims.OrgID)
	if err != nil {
		h.logger.ErrorContext(r.Context(), "list users", "error", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}

	out := make([]userResponse, 0, len(users))
	for _, u := range users {
		out = append(out, userResponse{ID: u.ID, OrgID: u.OrgID, Email: u.Email, Role: u.Role, Active: u.Active})
	}

	writeJSON(w, http.StatusOK, out)
}
