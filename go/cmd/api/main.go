// Command api is go-api: the REST control plane (docs/architecture.md
// §1.2). Auth, RBAC, audit logging, consultation CRUD, MinIO-backed
// uploads, job submission and status. Export and the review-facing edit
// endpoints are Phase 8.
package main

import (
	"context"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"coda/go/internal/audit"
	"coda/go/internal/auth"
	"coda/go/internal/config"
	"coda/go/internal/db"
	"coda/go/internal/db/sqlc"
	codahttp "coda/go/internal/http"
	"coda/go/internal/queue"
	"coda/go/internal/storage"
	"coda/go/internal/telemetry"
)

const serviceName = "go-api"

var version = "dev"

func main() {
	baseCfg := config.LoadBase(serviceName)
	logger := telemetry.NewLogger(baseCfg.ServiceName, baseCfg.LogLevel)

	// Load and validate every config group before touching the network —
	// fail fast on a missing or weak secret rather than on the first
	// request that needs it (task instructions; docs/architecture.md §0).
	dbCfg := config.LoadDB()
	authCfg := config.LoadAuth()
	serverCfg := config.LoadServer()
	storageCfg := config.LoadStorage()
	redisCfg := config.LoadRedis()
	if err := mustValidate(authCfg, serverCfg, storageCfg, redisCfg); err != nil {
		logger.Error("invalid configuration", "error", err)
		os.Exit(1)
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	pool, err := db.NewPool(ctx, dbCfg.DSN())
	if err != nil {
		logger.Error("connect to database", "error", err)
		os.Exit(1)
	}
	defer pool.Close()

	storageClient, err := storage.NewClient(ctx, storageCfg, baseCfg.Env)
	if err != nil {
		logger.Error("connect to object storage", "error", err)
		os.Exit(1)
	}

	queries := sqlc.New(pool)
	jwtSvc := auth.NewJWTService(authCfg.JWTSigningKey, authCfg.AccessTokenTTL)
	recorder := audit.NewSQLRecorder(queries)

	// The real Enqueuer, replacing the NoopEnqueuer stub
	// (claude_context.md decision #34 called this "a one-line change at
	// the call site"; it is). It rings go-orchestrator's doorbell on
	// job.submitted and dispatches nothing — go-api still XADDs to no
	// stage.* stream and consumes no stream at all (§1.2).
	//
	// Redis being unreachable does not stop go-api serving: the jobs row
	// is the durable record and the orchestrator's dispatch scan finds it
	// regardless, so submission degrades to "starts on the next scan"
	// rather than failing.
	var enqueuer queue.Enqueuer = queue.NoopEnqueuer{Logger: logger}
	queueClient, err := queue.NewClient(ctx, redisCfg, queue.Options{})
	if err != nil {
		logger.Warn("connect to redis for job dispatch signalling — falling back to orchestrator polling", "error", err)
	} else {
		defer func() { _ = queueClient.Close() }()
		enqueuer = queue.StreamEnqueuer{Client: queueClient, Logger: logger}
	}

	router := codahttp.NewRouter(codahttp.RouterDeps{
		ServiceName: serviceName,
		Version:     version,
		Pool:        pool,
		Queries:     queries,
		JWT:         jwtSvc,
		Recorder:    recorder,
		Logger:      logger,
		ServerCfg:   serverCfg,
		AuthCfg:     authCfg,
		Storage:     storageClient,
		Enqueuer:    enqueuer,
	})

	apiSrv := &http.Server{
		Addr:              ":" + serverCfg.HTTPPort,
		Handler:           router,
		ReadHeaderTimeout: 5 * time.Second,
	}
	metricsSrv := &http.Server{
		Addr:              ":" + serverCfg.MetricsPort,
		Handler:           codahttp.NewMetricsRouter(),
		ReadHeaderTimeout: 5 * time.Second,
	}

	go runServer(logger, "api", apiSrv)
	go runServer(logger, "metrics", metricsSrv)

	<-ctx.Done()
	logger.Info("shutting down")

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_ = apiSrv.Shutdown(shutdownCtx)
	_ = metricsSrv.Shutdown(shutdownCtx)
}

// mustValidate never receives or logs a secret value itself — Auth.Validate
// only inspects length, and any returned error string is built from field
// names and lengths, never JWTSigningKey (docs/architecture.md §0: "no
// secret ever logged").
func mustValidate(authCfg config.Auth, serverCfg config.Server, storageCfg config.Storage, redisCfg config.Redis) error {
	if err := authCfg.Validate(); err != nil {
		return err
	}
	if err := serverCfg.Validate(); err != nil {
		return err
	}
	if err := storageCfg.Validate(); err != nil {
		return err
	}
	if err := redisCfg.Validate(); err != nil {
		return err
	}
	return nil
}

func runServer(logger *slog.Logger, name string, srv *http.Server) {
	logger.Info("listening", "server", name, "addr", srv.Addr)
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		logger.Error("server failed", "server", name, "error", err)
		os.Exit(1)
	}
}
