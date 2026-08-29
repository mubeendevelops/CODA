// Command orchestrator is go-orchestrator: the pipeline state machine
// (docs/architecture.md §1.2, §4).
//
// It exposes no public API — only health and metrics on 8081 — because
// every input it takes arrives over Redis Streams or the jobs table. There
// is deliberately no HTTP route that dispatches, cancels, or retries a job:
// go-api owns the user-facing surface, and a second write path into the
// state machine would break the "one authority" property ADR-0006 exists
// for.
package main

import (
	"context"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/hibiken/asynq"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"

	"coda/go/internal/config"
	"coda/go/internal/db"
	"coda/go/internal/db/sqlc"
	codahttp "coda/go/internal/http"
	"coda/go/internal/pipeline"
	"coda/go/internal/queue"
	"coda/go/internal/storage"
	"coda/go/internal/tasks"
	"coda/go/internal/telemetry"
)

const serviceName = "go-orchestrator"

var version = "dev"

func main() {
	baseCfg := config.LoadBase(serviceName)
	logger := telemetry.NewLogger(baseCfg.ServiceName, baseCfg.LogLevel)

	dbCfg := config.LoadDB()
	redisCfg := config.LoadRedis()
	storageCfg := config.LoadStorage()
	if err := redisCfg.Validate(); err != nil {
		logger.Error("invalid configuration", "error", err)
		os.Exit(1)
	}
	if err := storageCfg.Validate(); err != nil {
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

	queueClient, err := queue.NewClient(ctx, redisCfg, queue.Options{})
	if err != nil {
		logger.Error("connect to redis", "error", err)
		os.Exit(1)
	}
	defer func() { _ = queueClient.Close() }()

	// Object storage is metadata-read-only here (§1.3): the orchestrator
	// stats a stage's output to index it, and never reads or writes the
	// bytes themselves.
	storageClient, err := storage.NewClient(ctx, storageCfg, baseCfg.Env)
	if err != nil {
		logger.Error("connect to object storage", "error", err)
		os.Exit(1)
	}

	// ConsumerName must be stable across restarts so this process
	// recognises its own PEL entries on recovery, and distinct across
	// replicas so two orchestrators do not claim the same consumer
	// identity. The hostname gives both properties inside Compose, where a
	// service's container name is stable.
	consumerName, _ := os.Hostname()
	if consumerName == "" {
		consumerName = "orchestrator"
	}

	orch := pipeline.New(pool, queueClient, storageClient, logger, pipeline.Config{
		ConsumerName: consumerName,
	})

	registry := prometheus.NewRegistry()
	for _, c := range orch.Metrics().Collectors() {
		registry.MustRegister(c)
	}

	asynqRedis := asynq.RedisClientOpt{Addr: redisCfg.Addr, Password: redisCfg.Password, DB: redisCfg.AsynqDB}
	taskHandlers := &tasks.Handlers{
		Queries: sqlc.New(pool), Queue: queueClient, Orchestrator: orch,
		Store: storageClient, Logger: logger, Metrics: orch.Metrics(),
	}
	asynqServer := asynq.NewServer(asynqRedis, asynq.Config{
		// One worker: these three jobs are periodic housekeeping, and
		// running two retention sweeps concurrently would have them race
		// to delete the same objects.
		Concurrency: 1,
		Queues:      map[string]int{tasks.QueueName: 1},
		Logger:      asynqLogger{logger},
	})
	mux := asynq.NewServeMux()
	taskHandlers.Register(mux)

	scheduler := asynq.NewScheduler(asynqRedis, &asynq.SchedulerOpts{Logger: asynqLogger{logger}})
	if err := tasks.RegisterPeriodic(scheduler, tasks.DefaultSchedule); err != nil {
		logger.Error("register periodic tasks", "error", err)
		os.Exit(1)
	}

	router := codahttp.NewHealthRouter(serviceName, version)
	router.Handle("/metrics", promhttp.HandlerFor(registry, promhttp.HandlerOpts{}))
	srv := &http.Server{
		Addr:              ":" + getenv("HEALTH_PORT", "8081"),
		Handler:           router,
		ReadHeaderTimeout: 5 * time.Second,
	}

	go func() {
		logger.Info("listening", "addr", srv.Addr)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			logger.Error("server failed", "error", err)
			os.Exit(1)
		}
	}()
	go func() {
		if err := asynqServer.Run(mux); err != nil {
			logger.Error("asynq server failed", "error", err)
		}
	}()
	go func() {
		if err := scheduler.Run(); err != nil {
			logger.Error("asynq scheduler failed", "error", err)
		}
	}()
	go func() {
		if err := orch.Run(ctx); err != nil {
			logger.Error("orchestrator failed", "error", err)
			os.Exit(1)
		}
	}()

	<-ctx.Done()
	logger.Info("shutting down")

	scheduler.Shutdown()
	asynqServer.Shutdown()

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

// asynqLogger adapts slog to Asynq's logging interface so orchestrator logs
// stay one structured stream (§8: "structured JSON, trace_id on every
// line").
type asynqLogger struct{ l *slog.Logger }

func (a asynqLogger) Debug(args ...any) { a.l.Debug("asynq", "msg", args) }
func (a asynqLogger) Info(args ...any)  { a.l.Info("asynq", "msg", args) }
func (a asynqLogger) Warn(args ...any)  { a.l.Warn("asynq", "msg", args) }
func (a asynqLogger) Error(args ...any) { a.l.Error("asynq", "msg", args) }
func (a asynqLogger) Fatal(args ...any) { a.l.Error("asynq fatal", "msg", args) }
