// Command orchestrator is go-orchestrator: the pipeline state machine
// (docs/architecture.md §1.2, §4). It exposes no public API — only health and
// metrics on 8081. Phase 0: health/readiness only; stage dispatch, retries,
// DLQ routing, and XAUTOCLAIM recovery land in Phase 3.
package main

import (
	"context"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	codahttp "coda/go/internal/http"
	"coda/go/internal/telemetry"

	"coda/go/internal/config"
)

const serviceName = "go-orchestrator"

var version = "dev"

func main() {
	cfg := config.LoadBase(serviceName)
	logger := telemetry.NewLogger(cfg.ServiceName, cfg.LogLevel)

	port := getenv("HEALTH_PORT", "8081")

	router := codahttp.NewHealthRouter(serviceName, version)
	router.Get("/metrics", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/plain; version=0.0.4")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("# Prometheus metrics land in Phase 3 (docs/architecture.md §8)\n"))
	})

	srv := &http.Server{
		Addr:              ":" + port,
		Handler:           router,
		ReadHeaderTimeout: 5 * time.Second,
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	go func() {
		logger.Info("listening", "addr", srv.Addr)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			logger.Error("server failed", "error", err)
			os.Exit(1)
		}
	}()

	<-ctx.Done()
	logger.Info("shutting down")

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_ = srv.Shutdown(shutdownCtx)
}

func getenv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
