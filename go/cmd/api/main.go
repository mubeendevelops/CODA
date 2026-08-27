// Command api is go-api: the REST control plane (docs/architecture.md §1.2).
// Phase 0: serves only health/readiness and a metrics stub. Auth, uploads,
// job submission, and the review/export endpoints land in Phase 3.
package main

import (
	"context"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	codahttp "coda/go/internal/http"
	"coda/go/internal/telemetry"

	"coda/go/internal/config"
)

const serviceName = "go-api"

var version = "dev"

func main() {
	cfg := config.LoadBase(serviceName)
	logger := telemetry.NewLogger(cfg.ServiceName, cfg.LogLevel)

	httpPort := getenv("HTTP_PORT", "8080")
	metricsPort := getenv("METRICS_PORT", "9090")

	apiSrv := &http.Server{
		Addr:              ":" + httpPort,
		Handler:           codahttp.NewHealthRouter(serviceName, version),
		ReadHeaderTimeout: 5 * time.Second,
	}
	metricsSrv := &http.Server{
		Addr:              ":" + metricsPort,
		Handler:           metricsStubHandler(),
		ReadHeaderTimeout: 5 * time.Second,
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	go runServer(logger, "api", apiSrv)
	go runServer(logger, "metrics", metricsSrv)

	<-ctx.Done()
	logger.Info("shutting down")

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_ = apiSrv.Shutdown(shutdownCtx)
	_ = metricsSrv.Shutdown(shutdownCtx)
}

func runServer(logger *slog.Logger, name string, srv *http.Server) {
	logger.Info("listening", "server", name, "addr", srv.Addr)
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		logger.Error("server failed", "server", name, "error", err)
		os.Exit(1)
	}
}

func metricsStubHandler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/plain; version=0.0.4")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("# Prometheus metrics land in Phase 3 (docs/architecture.md §8)\n"))
	})
}

func getenv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
