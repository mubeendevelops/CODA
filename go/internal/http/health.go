// Package http provides the shared healthz/readyz wiring used by go-api and
// go-orchestrator. go-api layers its full REST surface on top (Phase 3);
// go-orchestrator exposes only this — it has no public API
// (docs/architecture.md §1.1).
package http

import (
	"encoding/json"
	"net/http"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
)

// NewHealthRouter returns a chi.Router with /healthz and /readyz wired.
// serviceName and version are echoed in the response body so a curl during
// `docker compose ps` debugging shows which build answered.
func NewHealthRouter(serviceName, version string) chi.Router {
	r := chi.NewRouter()
	r.Use(middleware.Recoverer)

	body := map[string]string{
		"service": serviceName,
		"version": version,
		"status":  "ok",
	}

	handler := func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_ = json.NewEncoder(w).Encode(body)
	}

	r.Get("/healthz", handler)
	r.Get("/readyz", handler)

	return r
}
