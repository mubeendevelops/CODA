package http

import (
	"errors"
	"net/http"
	"strconv"

	"github.com/go-chi/chi/v5"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"

	"coda/go/internal/audit"
	"coda/go/internal/auth"
	"coda/go/internal/db/sqlc"
)

const (
	defaultPageLimit = 50
	maxPageLimit     = 200
)

type consultationResponse struct {
	ID       uuid.UUID `json:"id"`
	OrgID    uuid.UUID `json:"org_id"`
	State    string    `json:"state"`
	Language string    `json:"language"`
}

// ListConsultations returns consultations in the caller's own org only.
// Every reported number in this project depends on that being airtight
// (docs/architecture.md §7.6: "no cross-org read is expressible through
// the API") — the query takes claims.OrgID, never a caller-supplied org,
// so there is no request parameter that can widen the scope.
func (h *Handlers) ListConsultations(w http.ResponseWriter, r *http.Request) {
	claims, ok := auth.ClaimsFromContext(r.Context())
	if !ok {
		writeError(w, http.StatusUnauthorized, "authentication required")
		return
	}

	limit := int32(defaultPageLimit)
	if v := r.URL.Query().Get("limit"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 && n <= maxPageLimit {
			limit = int32(n)
		}
	}
	offset := int32(0)
	if v := r.URL.Query().Get("offset"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n >= 0 {
			offset = int32(n)
		}
	}

	rows, err := h.queries.ListConsultationsByOrgAndState(r.Context(), sqlc.ListConsultationsByOrgAndStateParams{
		OrgID:  claims.OrgID,
		Limit:  limit,
		Offset: offset,
		State:  pgtype.Text{}, // NULL: every state
	})
	if err != nil {
		h.logger.ErrorContext(r.Context(), "list consultations", "error", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}

	out := make([]consultationResponse, 0, len(rows))
	for _, c := range rows {
		out = append(out, consultationResponse{ID: c.ID, OrgID: c.OrgID, State: c.State, Language: c.Language})
	}

	audit.SetAction(r.Context(), "consultation.list")
	audit.MarkClinicalRead(r.Context())
	writeJSON(w, http.StatusOK, out)
}

// GetConsultation fetches one consultation by ID and enforces org scoping
// at the handler level in addition to the coarse role check the route
// middleware already applies (docs/architecture.md §7.6 — checked at both
// layers deliberately, not either/or). A consultation belonging to a
// different org than the caller's is reported as 404, matching
// RequireSameOrg's information-leak rationale.
func (h *Handlers) GetConsultation(w http.ResponseWriter, r *http.Request) {
	claims, ok := auth.ClaimsFromContext(r.Context())
	if !ok {
		writeError(w, http.StatusUnauthorized, "authentication required")
		return
	}

	id, err := uuid.Parse(chi.URLParam(r, "id"))
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid consultation id")
		return
	}

	c, err := h.queries.GetConsultation(r.Context(), id)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			writeError(w, http.StatusNotFound, "not found")
			return
		}
		h.logger.ErrorContext(r.Context(), "get consultation", "error", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}

	audit.SetResource(r.Context(), "consultation", c.ID.String())
	audit.SetAction(r.Context(), "consultation.read")
	audit.MarkClinicalRead(r.Context())

	if !auth.RequireSameOrg(w, claims, c.OrgID) {
		return
	}

	writeJSON(w, http.StatusOK, consultationResponse{ID: c.ID, OrgID: c.OrgID, State: c.State, Language: c.Language})
}
