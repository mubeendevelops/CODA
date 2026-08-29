package http

import (
	"log/slog"

	"coda/go/internal/audit"
	"coda/go/internal/auth"
	"coda/go/internal/config"
	"coda/go/internal/db/sqlc"
	"coda/go/internal/queue"
	"coda/go/internal/storage"
)

// Handlers holds every dependency the REST handlers need. Constructed once
// in NewRouter and shared across requests — nothing here is per-request
// state (that lives in the request context via auth.Claims).
type Handlers struct {
	queries  *sqlc.Queries
	jwt      *auth.JWTService
	recorder audit.Recorder
	logger   *slog.Logger
	authCfg  config.Auth
	storage  *storage.Client
	enqueuer queue.Enqueuer
}
