package http

import (
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"time"

	"github.com/go-chi/chi/v5/middleware"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	"coda/go/internal/audit"
	"coda/go/internal/auth"
	"coda/go/internal/db/sqlc"
)

const uniqueViolation = "23505"

type registerRequest struct {
	Email    string `json:"email"`
	Password string `json:"password"`
	Role     string `json:"role"`
}

type userResponse struct {
	ID     uuid.UUID `json:"id"`
	OrgID  uuid.UUID `json:"org_id"`
	Email  string    `json:"email"`
	Role   string    `json:"role"`
	Active bool      `json:"active"`
}

// Register creates a user in the caller's own org. Admin-only, enforced
// both by the route's auth.RequireRole(auth.RoleAdmin) middleware and here
// (the handler never trusts an org_id from the request body — it always
// creates the new user in claims.OrgID, so an admin cannot be tricked or
// misused into creating a user in another org even if the request shape
// changes later).
func (h *Handlers) Register(w http.ResponseWriter, r *http.Request) {
	claims, ok := auth.ClaimsFromContext(r.Context())
	if !ok {
		writeError(w, http.StatusUnauthorized, "authentication required")
		return
	}

	var req registerRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}
	req.Email = strings.TrimSpace(req.Email)
	if req.Email == "" || req.Password == "" {
		writeError(w, http.StatusBadRequest, "email and password are required")
		return
	}
	if len(req.Password) < 8 {
		writeError(w, http.StatusBadRequest, "password must be at least 8 characters")
		return
	}
	role := auth.Role(req.Role)
	if !role.IsValid() {
		writeError(w, http.StatusBadRequest, "role must be one of admin, doctor, reviewer, auditor")
		return
	}

	hash, err := auth.HashPassword(req.Password)
	if err != nil {
		h.logger.ErrorContext(r.Context(), "register: hash password", "error", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}

	user, err := h.queries.CreateUser(r.Context(), sqlc.CreateUserParams{
		OrgID:        claims.OrgID,
		Email:        req.Email,
		PasswordHash: hash,
		Role:         string(role),
	})
	if err != nil {
		var pgErr *pgconn.PgError
		if errors.As(err, &pgErr) && pgErr.Code == uniqueViolation {
			writeError(w, http.StatusConflict, "a user with this email already exists")
			return
		}
		h.logger.ErrorContext(r.Context(), "register: create user", "error", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}

	audit.SetResource(r.Context(), "user", user.ID.String())
	audit.SetAction(r.Context(), "user.register")

	writeJSON(w, http.StatusCreated, userResponse{
		ID: user.ID, OrgID: user.OrgID, Email: user.Email, Role: user.Role, Active: user.Active,
	})
}

type loginRequest struct {
	Email    string `json:"email"`
	Password string `json:"password"`
}

type tokenResponse struct {
	AccessToken  string `json:"access_token"`
	RefreshToken string `json:"refresh_token"`
	TokenType    string `json:"token_type"`
	ExpiresIn    int    `json:"expires_in"`
}

// Login is unauthenticated by definition, so it cannot run behind
// auth.Authenticate/audit.Middleware like the rest of /v1 — it records its
// own audit_log entry directly, and only when the email resolves to a real
// user (an unknown email has no org to attribute the attempt to; that
// narrow case is not audited, a documented limitation, not an oversight).
func (h *Handlers) Login(w http.ResponseWriter, r *http.Request) {
	var req loginRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}
	req.Email = strings.TrimSpace(req.Email)

	user, err := h.queries.GetUserByEmail(r.Context(), req.Email)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			writeError(w, http.StatusUnauthorized, "invalid email or password")
			return
		}
		h.logger.ErrorContext(r.Context(), "login: get user", "error", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}

	valid, verr := auth.VerifyPassword(user.PasswordHash, req.Password)
	success := verr == nil && valid && user.Active

	h.recordAuthEvent(r, "auth.login", user, success)

	if !success {
		writeError(w, http.StatusUnauthorized, "invalid email or password")
		return
	}

	h.issueTokenPair(w, r, user, uuid.New())
}

type refreshRequest struct {
	RefreshToken string `json:"refresh_token"`
}

// Refresh rotates a refresh token: the presented token is marked revoked
// and linked to a freshly minted replacement in the same family
// (go/migrations/000028_refresh_tokens.up.sql). Presenting a token that
// has *already* been rotated (replaced_by_id set) is treated as reuse —
// a signal the token leaked — and revokes the entire family, forcing
// every device on that chain to log in again, not just rejecting this
// one request.
func (h *Handlers) Refresh(w http.ResponseWriter, r *http.Request) {
	var req refreshRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}
	if req.RefreshToken == "" {
		writeError(w, http.StatusBadRequest, "refresh_token is required")
		return
	}

	hash := auth.HashRefreshToken(req.RefreshToken)
	row, err := h.queries.GetRefreshTokenByHash(r.Context(), hash)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			writeError(w, http.StatusUnauthorized, "invalid refresh token")
			return
		}
		h.logger.ErrorContext(r.Context(), "refresh: lookup token", "error", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}

	user, err := h.queries.GetUser(r.Context(), row.UserID)
	if err != nil {
		h.logger.ErrorContext(r.Context(), "refresh: get user", "error", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}

	if row.ReplacedByID.Valid {
		// Reuse of an already-rotated token: compromise signal, kill the family.
		if err := h.queries.RevokeRefreshTokenFamily(r.Context(), row.FamilyID); err != nil {
			h.logger.ErrorContext(r.Context(), "refresh: revoke family on reuse", "error", err)
		}
		h.recordAuthEvent(r, "auth.refresh_reuse_detected", user, false)
		writeError(w, http.StatusUnauthorized, "refresh token already used")
		return
	}
	if row.RevokedAt.Valid {
		h.recordAuthEvent(r, "auth.refresh", user, false)
		writeError(w, http.StatusUnauthorized, "refresh token revoked")
		return
	}
	if row.ExpiresAt.Time.Before(time.Now()) {
		h.recordAuthEvent(r, "auth.refresh", user, false)
		writeError(w, http.StatusUnauthorized, "refresh token expired")
		return
	}
	if !user.Active {
		if err := h.queries.RevokeRefreshTokenFamily(r.Context(), row.FamilyID); err != nil {
			h.logger.ErrorContext(r.Context(), "refresh: revoke family for inactive user", "error", err)
		}
		h.recordAuthEvent(r, "auth.refresh", user, false)
		writeError(w, http.StatusUnauthorized, "account is not active")
		return
	}

	newRawToken, newParams, err := h.newRefreshTokenRow(r, user.ID, row.FamilyID)
	if err != nil {
		h.logger.ErrorContext(r.Context(), "refresh: create rotated token", "error", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}
	newRow, err := h.queries.CreateRefreshToken(r.Context(), *newParams)
	if err != nil {
		h.logger.ErrorContext(r.Context(), "refresh: persist rotated token", "error", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}
	if err := h.queries.RotateRefreshToken(r.Context(), sqlc.RotateRefreshTokenParams{
		ID:           row.ID,
		ReplacedByID: pgUUIDValid(newRow.ID),
	}); err != nil {
		h.logger.ErrorContext(r.Context(), "refresh: mark old token rotated", "error", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}

	accessToken, err := h.jwt.IssueAccessToken(user.ID, user.OrgID, auth.Role(user.Role))
	if err != nil {
		h.logger.ErrorContext(r.Context(), "refresh: issue access token", "error", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}

	h.recordAuthEvent(r, "auth.refresh", user, true)

	writeJSON(w, http.StatusOK, tokenResponse{
		AccessToken:  accessToken,
		RefreshToken: newRawToken,
		TokenType:    "Bearer",
		ExpiresIn:    int(h.authCfg.AccessTokenTTL.Seconds()),
	})
}

// Logout revokes exactly the presented refresh token (not its whole
// family) — logging out one device must not force every other device on
// the same account to re-authenticate. It is idempotent and always
// responds 200, whether or not the token was found, so a client cannot
// use the response to probe token validity.
func (h *Handlers) Logout(w http.ResponseWriter, r *http.Request) {
	var req refreshRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}

	if req.RefreshToken != "" {
		hash := auth.HashRefreshToken(req.RefreshToken)
		if row, err := h.queries.GetRefreshTokenByHash(r.Context(), hash); err == nil {
			if err := h.queries.RevokeRefreshToken(r.Context(), row.ID); err != nil {
				h.logger.ErrorContext(r.Context(), "logout: revoke token", "error", err)
			} else if user, err := h.queries.GetUser(r.Context(), row.UserID); err == nil {
				h.recordAuthEvent(r, "auth.logout", user, true)
			}
		} else if !errors.Is(err, pgx.ErrNoRows) {
			h.logger.ErrorContext(r.Context(), "logout: lookup token", "error", err)
		}
	}

	writeJSON(w, http.StatusOK, map[string]string{"status": "logged_out"})
}

func (h *Handlers) issueTokenPair(w http.ResponseWriter, r *http.Request, user *sqlc.User, familyID uuid.UUID) {
	accessToken, err := h.jwt.IssueAccessToken(user.ID, user.OrgID, auth.Role(user.Role))
	if err != nil {
		h.logger.ErrorContext(r.Context(), "issue access token", "error", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}

	rawRefresh, params, err := h.newRefreshTokenRow(r, user.ID, familyID)
	if err != nil {
		h.logger.ErrorContext(r.Context(), "issue refresh token", "error", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}
	if _, err := h.queries.CreateRefreshToken(r.Context(), *params); err != nil {
		h.logger.ErrorContext(r.Context(), "persist refresh token", "error", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}

	writeJSON(w, http.StatusOK, tokenResponse{
		AccessToken:  accessToken,
		RefreshToken: rawRefresh,
		TokenType:    "Bearer",
		ExpiresIn:    int(h.authCfg.AccessTokenTTL.Seconds()),
	})
}

func (h *Handlers) newRefreshTokenRow(r *http.Request, userID, familyID uuid.UUID) (string, *sqlc.CreateRefreshTokenParams, error) {
	raw, err := auth.GenerateRefreshToken()
	if err != nil {
		return "", nil, err
	}
	params := &sqlc.CreateRefreshTokenParams{
		UserID:      userID,
		FamilyID:    familyID,
		TokenHash:   auth.HashRefreshToken(raw),
		ExpiresAt:   toTimestamptz(time.Now().Add(h.authCfg.RefreshTokenTTL)),
		CreatedByIp: clientIP(r.RemoteAddr),
		UserAgent:   pgTextOrNull(r.UserAgent()),
	}
	return raw, params, nil
}

// recordAuthEvent writes an audit_log entry directly (not via
// audit.Middleware, which requires an already-authenticated request
// context that login/refresh/logout by definition don't have).
func (h *Handlers) recordAuthEvent(r *http.Request, action string, user *sqlc.User, success bool) {
	outcome := audit.OutcomeSuccess
	if !success {
		outcome = audit.OutcomeFailure
	}
	userID := user.ID
	err := h.recorder.Record(r.Context(), audit.Entry{
		OrgID:        user.OrgID,
		ActorUserID:  &userID,
		Action:       action,
		ResourceType: "user",
		ResourceID:   user.ID.String(),
		TraceID:      middleware.GetReqID(r.Context()),
		IP:           clientIP(r.RemoteAddr),
		Outcome:      outcome,
	})
	if err != nil {
		h.logger.ErrorContext(r.Context(), "audit: failed to record auth event", "error", err, "action", action)
	}
}
