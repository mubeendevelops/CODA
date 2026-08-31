package http

import (
	"context"
	"log/slog"
	"net/http"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
	"github.com/go-chi/cors"
	"github.com/go-chi/httprate"
	"github.com/jackc/pgx/v5/pgxpool"

	"coda/go/internal/audit"
	"coda/go/internal/auth"
	"coda/go/internal/config"
	"coda/go/internal/db/sqlc"
	"coda/go/internal/queue"
	"coda/go/internal/storage"
)

// RouterDeps bundles everything the REST router needs to construct its
// handlers and middleware stack. Constructed once at startup in cmd/api.
type RouterDeps struct {
	ServiceName string
	Version     string
	Pool        *pgxpool.Pool
	Queries     *sqlc.Queries
	JWT         *auth.JWTService
	Recorder    audit.Recorder
	Logger      *slog.Logger
	ServerCfg   config.Server
	AuthCfg     config.Auth
	// Storage may be nil in tests that don't exercise the audio
	// upload/delete paths — handlers that need it check explicitly and
	// fail loudly rather than panic (see consultations_handlers.go).
	Storage  *storage.Client
	Enqueuer queue.Enqueuer
}

// NewRouter builds go-api's REST router (docs/architecture.md §1.1, §7.6):
// request ID, structured logging, panic recovery, CORS, request timeout,
// per-request metrics, rate limiting, then JWT auth + RBAC + audit logging
// on every route under /v1. /metrics is deliberately not mounted here — it
// is served on a separate port (NewMetricsRouter) per architecture spec
// §1.1, so scraping is never subject to the API's auth or rate limits.
func NewRouter(d RouterDeps) chi.Router {
	r := chi.NewRouter()

	r.Use(middleware.RequestID)
	// Trust model, stated explicitly (httprate.KeyByIP/LimitByIP are
	// deprecated for leaving this implicit): go-api is directly exposed to
	// clients in this project's topology (docs/architecture.md §1.1 — no
	// reverse proxy in front), so the TCP peer address is itself the real
	// client IP. ClientIPFromRemoteAddr documents and enforces that
	// assumption; switching to a fronting proxy later means swapping this
	// one line for middleware.ClientIPFromXFF/Header, not silently trusting
	// a spoofable header.
	r.Use(middleware.ClientIPFromRemoteAddr)
	r.Use(RequestLogger(d.Logger))
	r.Use(Recoverer(d.Logger))
	r.Use(cors.Handler(cors.Options{
		AllowedOrigins:   d.ServerCfg.CORSAllowedOrigins,
		AllowedMethods:   []string{http.MethodGet, http.MethodPost, http.MethodPut, http.MethodPatch, http.MethodDelete, http.MethodOptions},
		AllowedHeaders:   []string{"Authorization", "Content-Type"},
		AllowCredentials: true,
		MaxAge:           300,
	}))
	r.Use(middleware.Timeout(d.ServerCfg.RequestTimeout))
	r.Use(MetricsMiddleware())
	r.Use(httprate.LimitBy(d.ServerCfg.RateLimitRequests, d.ServerCfg.RateLimitWindow, rateLimitKey))

	r.Get("/healthz", healthzHandler(d.ServiceName, d.Version))
	r.Get("/readyz", readyzHandler(d.Pool))

	h := &Handlers{
		queries:  d.Queries,
		jwt:      d.JWT,
		recorder: d.Recorder,
		logger:   d.Logger,
		authCfg:  d.AuthCfg,
		storage:  d.Storage,
		enqueuer: d.Enqueuer,
	}

	r.Route("/v1/auth", func(r chi.Router) {
		// Stricter, separate rate-limit bucket: this is the
		// brute-force/credential-stuffing surface (docs/architecture.md
		// §7.6 "rate limiting on upload and auth endpoints").
		r.Use(httprate.LimitBy(d.ServerCfg.AuthRateLimitRequests, d.ServerCfg.AuthRateLimitWindow, rateLimitKey))

		r.Post("/login", h.Login)
		r.Post("/refresh", h.Refresh)
		r.Post("/logout", h.Logout)

		r.Group(func(r chi.Router) {
			r.Use(auth.Authenticate(d.JWT))
			r.Use(audit.Middleware(d.Recorder, d.Logger))
			r.Use(auth.RequireRole(auth.RoleAdmin))
			r.Post("/register", h.Register)
		})
	})

	r.Route("/v1", func(r chi.Router) {
		r.Use(auth.Authenticate(d.JWT))
		r.Use(audit.Middleware(d.Recorder, d.Logger))

		r.With(auth.RequireRole(auth.RoleAdmin)).
			Get("/users", h.ListUsers)

		r.With(auth.RequireRole(auth.RoleAdmin, auth.RoleDoctor, auth.RoleReviewer)).
			Get("/consultations", h.ListConsultations)
		r.With(auth.RequireRole(auth.RoleAdmin, auth.RoleDoctor)).
			Post("/consultations", h.CreateConsultation)
		r.With(auth.RequireRole(auth.RoleAdmin, auth.RoleDoctor, auth.RoleReviewer)).
			Get("/consultations/{id}", h.GetConsultation)
		r.With(auth.RequireRole(auth.RoleAdmin, auth.RoleDoctor)).
			Delete("/consultations/{id}", h.DeleteConsultation)
		r.With(auth.RequireRole(auth.RoleAdmin, auth.RoleDoctor)).
			Post("/consultations/{id}/audio/presign", h.PresignConsultationAudio)
		r.With(auth.RequireRole(auth.RoleAdmin, auth.RoleDoctor)).
			Post("/consultations/{id}/audio/confirm", h.ConfirmConsultationAudio)
		r.With(auth.RequireRole(auth.RoleAdmin, auth.RoleDoctor)).
			Post("/consultations/{id}/jobs", h.CreateJob)
		r.With(auth.RequireRole(auth.RoleAdmin, auth.RoleDoctor)).
			Post("/consultations/{id}/cancel", h.CancelConsultation)
		r.With(auth.RequireRole(auth.RoleAdmin, auth.RoleDoctor, auth.RoleReviewer)).
			Get("/consultations/{id}/result", h.GetConsultationResult)
		r.With(auth.RequireRole(auth.RoleAdmin, auth.RoleDoctor, auth.RoleReviewer)).
			Get("/consultations/{id}/transcript", h.PresignConsultationTranscript)
		r.With(auth.RequireRole(auth.RoleAdmin, auth.RoleDoctor, auth.RoleReviewer)).
			Get("/jobs/{id}", h.GetJob)
		r.With(auth.RequireRole(auth.RoleAdmin, auth.RoleDoctor, auth.RoleReviewer)).
			Get("/jobs/{id}/events", h.JobEvents)

		r.With(auth.RequireRole(auth.RoleAdmin, auth.RoleAuditor)).
			Get("/audit-log", h.ListAuditLog)
	})

	return r
}

// NewMetricsRouter serves only /metrics, meant for the separate metrics
// port (docs/architecture.md §1.1).
func NewMetricsRouter() chi.Router {
	r := chi.NewRouter()
	r.Handle("/metrics", NewMetricsHandler())
	return r
}

// rateLimitKey buckets by the resolved client IP that
// middleware.ClientIPFromRemoteAddr placed in context — see the trust-model
// comment on that middleware's mount point in NewRouter.
func rateLimitKey(r *http.Request) (string, error) {
	return httprate.CanonicalizeIP(middleware.GetClientIP(r.Context())), nil
}

func healthzHandler(serviceName, version string) http.HandlerFunc {
	return func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"service": serviceName, "version": version, "status": "ok"})
	}
}

// readyzHandler additionally verifies the database is reachable — a go-api
// instance that can accept connections but can't reach Postgres should not
// be reported ready to a load balancer or orchestrator.
func readyzHandler(pool *pgxpool.Pool) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
		defer cancel()
		if err := pool.Ping(ctx); err != nil {
			writeError(w, http.StatusServiceUnavailable, "database not ready")
			return
		}
		writeJSON(w, http.StatusOK, map[string]string{"status": "ready"})
	}
}
