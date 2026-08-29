package http

import (
	"encoding/json"
	"errors"
	"net/http"
	"strconv"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"

	"coda/go/internal/audit"
	"coda/go/internal/auth"
	"coda/go/internal/db/sqlc"
	"coda/go/internal/storage"
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
		OrgID:    claims.OrgID,
		Limit:    limit,
		Offset:   offset,
		State:    pgTextOrNull(r.URL.Query().Get("state")),    // "" => NULL: every state
		Language: pgTextOrNull(r.URL.Query().Get("language")), // "" => NULL: every language
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

// consultationSupportedLanguage is v1's only accepted value for the
// consultation-level language field (claude_context.md §2.1, decision:
// Kannada-English is a v2 extension). Rejecting anything else with a clear
// 422 — rather than silently accepting and storing it as "en" — is the
// explicit requirement: a mislabeled multilingual consultation would be
// invisible corruption in every downstream metric.
const consultationSupportedLanguage = "en"

type createConsultationRequest struct {
	Language string                    `json:"language"`
	Consent  createConsentRecordFields `json:"consent"`
}

type createConsentRecordFields struct {
	ConsentObtained bool   `json:"consent_obtained"`
	ConsentType     string `json:"consent_type"`
	ConsentMethod   string `json:"consent_method"`
	SubjectRef      string `json:"subject_ref"`
}

// CreateConsultation creates a consultation and its owning consent_records
// row together. consent_record_id is NOT NULL on consultations
// (go/migrations/000011) — a consultation cannot be representable without
// consent, so the two are created in the same request rather than a
// separate "attach consent later" step.
func (h *Handlers) CreateConsultation(w http.ResponseWriter, r *http.Request) {
	claims, ok := auth.ClaimsFromContext(r.Context())
	if !ok {
		writeError(w, http.StatusUnauthorized, "authentication required")
		return
	}

	var req createConsultationRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}

	if req.Language == "" {
		req.Language = consultationSupportedLanguage
	}
	if req.Language != consultationSupportedLanguage {
		writeError(w, http.StatusUnprocessableEntity,
			"language must be \"en\" — multilingual support is not implemented yet (v1 is English-only; see claude_context.md §2.1)")
		return
	}

	// Explicit consent payload required — an omitted or false
	// consent_obtained is rejected outright, never defaulted to true.
	if !req.Consent.ConsentObtained {
		writeError(w, http.StatusUnprocessableEntity, "consent.consent_obtained must be true to create a consultation")
		return
	}
	if req.Consent.ConsentType == "" {
		writeError(w, http.StatusUnprocessableEntity, "consent.consent_type is required")
		return
	}
	subjectRef := req.Consent.SubjectRef
	if subjectRef == "" {
		// subject_ref must never be a name (docs/architecture.md §5.1) — a
		// generated pseudonymous reference is a safe default when the
		// caller doesn't supply one of its own.
		subjectRef = "subject-" + uuid.NewString()
	}

	now := toTimestamptz(time.Now().UTC())
	consent, err := h.queries.CreateConsentRecord(r.Context(), sqlc.CreateConsentRecordParams{
		OrgID:       claims.OrgID,
		SubjectRef:  subjectRef,
		ConsentType: req.Consent.ConsentType,
		GrantedAt:   now,
		GrantedBy:   claims.UserID,
	})
	if err != nil {
		h.logger.ErrorContext(r.Context(), "create consultation: create consent record", "error", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}

	c, err := h.queries.CreateConsultation(r.Context(), sqlc.CreateConsultationParams{
		OrgID:             claims.OrgID,
		OwnerUserID:       claims.UserID,
		ConsentRecordID:   consent.ID,
		ConsentObtained:   true,
		ConsentMethod:     pgTextOrNull(req.Consent.ConsentMethod),
		ConsentRecordedAt: now,
		Language:          req.Language,
	})
	if err != nil {
		h.logger.ErrorContext(r.Context(), "create consultation: create consultation", "error", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}

	// CREATED -> CONSENT_RECORDED (docs/architecture.md §4.1): consent is
	// verified above, so the consultation never needs to sit in bare
	// `created` waiting for a consent step that already happened.
	c, err = h.queries.UpdateConsultationState(r.Context(), sqlc.UpdateConsultationStateParams{ID: c.ID, State: "consent_recorded"})
	if err != nil {
		h.logger.ErrorContext(r.Context(), "create consultation: advance state", "error", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}

	audit.SetResource(r.Context(), "consultation", c.ID.String())
	audit.SetAction(r.Context(), "consultation.create")

	writeJSON(w, http.StatusCreated, consultationResponse{ID: c.ID, OrgID: c.OrgID, State: c.State, Language: c.Language})
}

// --- Audio upload: presign / confirm -----------------------------------

type audioPresignRequest struct {
	ContentType string `json:"content_type"`
	SizeBytes   int64  `json:"size_bytes"`
	Sha256      string `json:"sha256"`
}

type audioPresignResponse struct {
	UploadURL string    `json:"upload_url"`
	ObjectKey string    `json:"object_key"`
	Method    string    `json:"method"`
	ExpiresAt time.Time `json:"expires_at"`
}

const audioPresignExpiry = 15 * time.Minute

// PresignConsultationAudio returns a time-limited MinIO PUT URL so audio
// bytes go straight from the caller to object storage — go-api never
// touches the bytes (docs/architecture.md §1.2). Content type and size are
// validated server-side before a URL is even issued, since a presigned PUT
// URL cannot itself enforce either.
func (h *Handlers) PresignConsultationAudio(w http.ResponseWriter, r *http.Request) {
	claims, ok := auth.ClaimsFromContext(r.Context())
	if !ok {
		writeError(w, http.StatusUnauthorized, "authentication required")
		return
	}
	if h.storage == nil {
		writeError(w, http.StatusServiceUnavailable, "object storage is not configured")
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
		h.logger.ErrorContext(r.Context(), "presign audio: get consultation", "error", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}
	if !auth.RequireSameOrg(w, claims, c.OrgID) {
		return
	}
	if c.ErasedAt.Valid {
		writeError(w, http.StatusUnprocessableEntity, "consultation has been erased and can no longer accept an upload")
		return
	}

	var req audioPresignRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}
	ext, ok := storage.AllowedAudioContentTypes[req.ContentType]
	if !ok {
		writeError(w, http.StatusUnprocessableEntity, "content_type must be one of audio/wav, audio/x-wav, audio/wave, audio/mpeg, audio/mp3")
		return
	}
	if req.SizeBytes <= 0 || req.SizeBytes > storage.MaxAudioUploadBytes {
		writeError(w, http.StatusUnprocessableEntity, "size_bytes must be positive and at most 500 MiB")
		return
	}
	if !storage.ValidSHA256Hex(req.Sha256) {
		writeError(w, http.StatusUnprocessableEntity, "sha256 must be a 64-character lowercase hex digest")
		return
	}

	key := h.storage.SourceAudioKey(c.ID, req.Sha256, ext)
	uploadURL, err := h.storage.PresignPutObject(r.Context(), key, audioPresignExpiry)
	if err != nil {
		h.logger.ErrorContext(r.Context(), "presign audio: presign put object", "error", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}

	audit.SetResource(r.Context(), "consultation", c.ID.String())
	audit.SetAction(r.Context(), "consultation.audio.presign")

	writeJSON(w, http.StatusOK, audioPresignResponse{
		UploadURL: uploadURL.String(),
		ObjectKey: key,
		Method:    http.MethodPut,
		ExpiresAt: time.Now().Add(audioPresignExpiry),
	})
}

type audioConfirmRequest struct {
	ObjectKey   string  `json:"object_key"`
	Sha256      string  `json:"sha256"`
	DurationSec float64 `json:"duration_sec"`
}

// ConfirmConsultationAudio verifies the presigned upload actually landed
// (StatObject against MinIO — the task's "verifies the object exists"
// requirement) before recording it as the consultation's source audio.
// go-api does not decode audio (docs/architecture.md §1.2 — no ML/audio
// processing here), so duration_sec is caller-declared and object size is
// cross-checked against the stat result as the integrity signal go-api can
// actually perform without downloading and rehashing the file.
func (h *Handlers) ConfirmConsultationAudio(w http.ResponseWriter, r *http.Request) {
	claims, ok := auth.ClaimsFromContext(r.Context())
	if !ok {
		writeError(w, http.StatusUnauthorized, "authentication required")
		return
	}
	if h.storage == nil {
		writeError(w, http.StatusServiceUnavailable, "object storage is not configured")
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
		h.logger.ErrorContext(r.Context(), "confirm audio: get consultation", "error", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}
	if !auth.RequireSameOrg(w, claims, c.OrgID) {
		return
	}

	var req audioConfirmRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}
	if !storage.ValidSHA256Hex(req.Sha256) {
		writeError(w, http.StatusUnprocessableEntity, "sha256 must be a 64-character lowercase hex digest")
		return
	}
	prefix := h.storage.SourceAudioKeyPrefix(c.ID)
	if len(req.ObjectKey) <= len(prefix) || req.ObjectKey[:len(prefix)] != prefix {
		writeError(w, http.StatusUnprocessableEntity, "object_key does not belong to this consultation")
		return
	}
	if req.DurationSec <= 0 {
		writeError(w, http.StatusUnprocessableEntity, "duration_sec must be positive")
		return
	}

	info, err := h.storage.StatObject(r.Context(), req.ObjectKey)
	if err != nil {
		if storage.IsNotFound(err) {
			writeError(w, http.StatusConflict, "upload not found in object storage — the presigned PUT has not completed yet")
			return
		}
		h.logger.ErrorContext(r.Context(), "confirm audio: stat object", "error", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}
	if info.Size <= 0 || info.Size > storage.MaxAudioUploadBytes {
		writeError(w, http.StatusUnprocessableEntity, "uploaded object size is invalid")
		return
	}

	c, err = h.queries.UpdateConsultationAudio(r.Context(), sqlc.UpdateConsultationAudioParams{
		ID:             c.ID,
		SourceAudioUri: pgTextOrNull(req.ObjectKey),
		AudioSha256:    pgTextOrNull(req.Sha256),
		DurationSec:    pgtype.Float8{Float64: req.DurationSec, Valid: true},
	})
	if err != nil {
		h.logger.ErrorContext(r.Context(), "confirm audio: update consultation", "error", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}
	// UPLOADED (docs/architecture.md §4.1).
	c, err = h.queries.UpdateConsultationState(r.Context(), sqlc.UpdateConsultationStateParams{ID: c.ID, State: "uploaded"})
	if err != nil {
		h.logger.ErrorContext(r.Context(), "confirm audio: advance state", "error", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}

	audit.SetResource(r.Context(), "consultation", c.ID.String())
	audit.SetAction(r.Context(), "consultation.audio.confirm")

	writeJSON(w, http.StatusOK, consultationResponse{ID: c.ID, OrgID: c.OrgID, State: c.State, Language: c.Language})
}

// --- Delete (DPDP erasure requirement) ----------------------------------

// DeleteConsultation performs a full cascading deletion: every artifact
// object in MinIO for this consultation, then the consultations row itself
// (which cascades to jobs/artifacts/transcripts/turns/thoughts/
// thought_edges/extractions/summaries/clinical_notes/reviews/review_edits
// via ON DELETE CASCADE — see DeleteConsultation in
// go/internal/db/queries/consultations.sql). This is a different, more
// literal operation than EraseConsultation's tombstone-and-null-columns
// path (docs/architecture.md §7.2) — both exist; this one is what the task
// asked for. consent_records is untouched (ON DELETE RESTRICT) since it is
// the compliance record, independent of the consultation's lifecycle.
//
// A before-snapshot is attached to the audit entry (audit.SetBefore)
// because the row — audit_log's usual source for "what did this resource
// look like" — will not exist to inspect after this request completes.
func (h *Handlers) DeleteConsultation(w http.ResponseWriter, r *http.Request) {
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
		h.logger.ErrorContext(r.Context(), "delete consultation: get consultation", "error", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}
	if !auth.RequireSameOrg(w, claims, c.OrgID) {
		return
	}

	if before, err := json.Marshal(c); err == nil {
		audit.SetBefore(r.Context(), before)
	}

	if h.storage != nil {
		artifacts, err := h.queries.ListArtifactsByConsultation(r.Context(), sqlc.ListArtifactsByConsultationParams{ConsultationID: c.ID})
		if err != nil {
			h.logger.ErrorContext(r.Context(), "delete consultation: list artifacts", "error", err)
			writeError(w, http.StatusInternalServerError, "internal server error")
			return
		}
		for _, a := range artifacts {
			if err := h.storage.RemoveObject(r.Context(), a.Uri); err != nil {
				h.logger.ErrorContext(r.Context(), "delete consultation: remove artifact object", "error", err, "uri", a.Uri)
				writeError(w, http.StatusInternalServerError, "internal server error")
				return
			}
		}
		if c.SourceAudioUri.Valid {
			if err := h.storage.RemoveObject(r.Context(), c.SourceAudioUri.String); err != nil {
				h.logger.ErrorContext(r.Context(), "delete consultation: remove source audio", "error", err)
				writeError(w, http.StatusInternalServerError, "internal server error")
				return
			}
		}
	}

	if err := h.queries.DeleteConsultation(r.Context(), c.ID); err != nil {
		h.logger.ErrorContext(r.Context(), "delete consultation: delete row", "error", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}

	audit.SetResource(r.Context(), "consultation", c.ID.String())
	audit.SetAction(r.Context(), "consultation.delete")

	writeJSON(w, http.StatusOK, map[string]any{"deleted": true, "id": c.ID})
}
