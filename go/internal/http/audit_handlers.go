package http

import (
	"net/http"
	"strconv"

	"github.com/google/uuid"

	"coda/go/internal/auth"
	"coda/go/internal/db/sqlc"
)

type auditLogEntryResponse struct {
	ID           uuid.UUID  `json:"id"`
	OrgID        uuid.UUID  `json:"org_id"`
	ActorUserID  *uuid.UUID `json:"actor_user_id,omitempty"`
	ActorService string     `json:"actor_service,omitempty"`
	Action       string     `json:"action"`
	ResourceType string     `json:"resource_type"`
	ResourceID   string     `json:"resource_id"`
	Outcome      string     `json:"outcome"`
}

// ListAuditLog returns audit_log entries for the caller's own org.
// admin/auditor only (route middleware) — auditor's entire access surface
// is this endpoint plus de-identified aggregates, never source audio,
// transcripts, or clinical note content (auth.Role.CanReadClinicalData).
func (h *Handlers) ListAuditLog(w http.ResponseWriter, r *http.Request) {
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

	rows, err := h.queries.ListAuditLogByOrg(r.Context(), sqlc.ListAuditLogByOrgParams{
		OrgID:  claims.OrgID,
		Limit:  limit,
		Offset: offset,
	})
	if err != nil {
		h.logger.ErrorContext(r.Context(), "list audit log", "error", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}

	out := make([]auditLogEntryResponse, 0, len(rows))
	for _, e := range rows {
		item := auditLogEntryResponse{
			ID: e.ID, OrgID: e.OrgID,
			Action: e.Action, ResourceType: e.ResourceType, ResourceID: e.ResourceID,
			Outcome: e.Outcome,
		}
		if e.ActorUserID.Valid {
			id := uuid.UUID(e.ActorUserID.Bytes)
			item.ActorUserID = &id
		}
		if e.ActorService.Valid {
			item.ActorService = e.ActorService.String
		}
		out = append(out, item)
	}

	writeJSON(w, http.StatusOK, out)
}
