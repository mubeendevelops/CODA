//go:build integration

// Integration harness for the queue transport and the orchestrator state
// machine, against a **real Redis and a real Postgres** — no mocks, per the
// task instructions and for a specific reason: every property under test
// here (XAUTOCLAIM reclaiming a dead worker's PEL entry, a UNIQUE
// constraint absorbing a duplicate, a transaction rolling a half-applied
// transition back) is a property of Redis and Postgres themselves. A mock
// would be asserting that the test double behaves the way the author
// assumed the real system does, which is the assumption most worth testing.
//
// Run with: cd go && go test -tags=integration ./internal/pipeline/...
// (Makefile target: `make test-integration`.) Requires a working Docker
// daemon and the `migrate` CLI on PATH.
package pipeline

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"os/exec"
	"path/filepath"
	"runtime"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/redis/go-redis/v9"
	"github.com/testcontainers/testcontainers-go"
	tcpostgres "github.com/testcontainers/testcontainers-go/modules/postgres"
	tcredis "github.com/testcontainers/testcontainers-go/modules/redis"
	"github.com/testcontainers/testcontainers-go/wait"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/types/known/timestamppb"

	"coda/go/internal/db"
	"coda/go/internal/db/sqlc"
	codev1 "coda/go/internal/genproto/coda/v1"
	"coda/go/internal/queue"
	"coda/go/internal/storage"
)

type harness struct {
	ctx     context.Context
	pool    *pgxpool.Pool
	queries *sqlc.Queries
	rdb     *redis.Client
	q       *queue.Client
	logger  *slog.Logger

	org          *sqlc.Org
	user         *sqlc.User
	consent      *sqlc.ConsentRecord
	consultation *sqlc.Consultation
}

func newHarness(t *testing.T) *harness {
	t.Helper()
	ctx := context.Background()

	redisContainer, err := tcredis.Run(ctx, "redis:7")
	if err != nil {
		t.Fatalf("start redis container: %v", err)
	}
	t.Cleanup(func() {
		if err := redisContainer.Terminate(context.Background()); err != nil {
			t.Logf("terminate redis container: %v", err)
		}
	})
	redisURI, err := redisContainer.ConnectionString(ctx)
	if err != nil {
		t.Fatalf("redis connection string: %v", err)
	}
	redisOpts, err := redis.ParseURL(redisURI)
	if err != nil {
		t.Fatalf("parse redis url %q: %v", redisURI, err)
	}
	rdb := redis.NewClient(redisOpts)
	t.Cleanup(func() { _ = rdb.Close() })
	if err := rdb.Ping(ctx).Err(); err != nil {
		t.Fatalf("ping redis: %v", err)
	}

	pgContainer, err := tcpostgres.Run(ctx, "postgres:16",
		tcpostgres.WithDatabase("coda_test"),
		tcpostgres.WithUsername("coda_test"),
		tcpostgres.WithPassword("coda_test"),
		// tmpfs for the data directory: this repo's working tree (and
		// Docker's data root on this machine) sits on an NTFS/fuseblk
		// volume that cannot report Unix ownership, which makes Postgres
		// refuse to start on a normal volume. Same workaround as
		// scripts/migrate_test.sh and internal/http's suite; also simply
		// the right choice for a throwaway container.
		testcontainers.WithTmpfs(map[string]string{"/var/lib/postgresql/data": ""}),
		testcontainers.WithWaitStrategy(
			wait.ForLog("database system is ready to accept connections").
				WithOccurrence(2).WithStartupTimeout(90*time.Second),
		),
	)
	if err != nil {
		t.Fatalf("start postgres container: %v", err)
	}
	t.Cleanup(func() {
		if err := pgContainer.Terminate(context.Background()); err != nil {
			t.Logf("terminate postgres container: %v", err)
		}
	})

	dsn, err := pgContainer.ConnectionString(ctx, "sslmode=disable")
	if err != nil {
		t.Fatalf("connection string: %v", err)
	}
	runMigrations(t, dsn)

	pool, err := db.NewPool(ctx, dsn)
	if err != nil {
		t.Fatalf("connect pool: %v", err)
	}
	t.Cleanup(pool.Close)

	h := &harness{
		ctx: ctx, pool: pool, queries: sqlc.New(pool), rdb: rdb,
		q:      queue.NewClientFromRedis(rdb, queue.Options{}),
		logger: slog.New(slog.NewTextHandler(io.Discard, nil)),
	}
	if err := h.q.EnsureGroups(ctx); err != nil {
		t.Fatalf("ensure consumer groups: %v", err)
	}
	h.seedTenant(t)
	return h
}

func runMigrations(t *testing.T, dsn string) {
	t.Helper()
	migrateBin, err := exec.LookPath("migrate")
	if err != nil {
		t.Fatalf("golang-migrate CLI not found on PATH: %v (the same tool `make migrate`/`make migrate-test` use)", err)
	}
	_, thisFile, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("could not determine test file location")
	}
	migrationsDir := filepath.Join(filepath.Dir(thisFile), "..", "..", "migrations")
	out, err := exec.Command(migrateBin, "-path", migrationsDir, "-database", dsn, "up").CombinedOutput()
	if err != nil {
		t.Fatalf("run migrations: %v\n%s", err, out)
	}
}

// seedTenant creates the org/user/consent/consultation every test needs.
// The consultation is left with confirmed source audio so ASR is
// immediately dispatchable.
func (h *harness) seedTenant(t *testing.T) {
	t.Helper()
	var err error
	h.org, err = h.queries.CreateOrg(h.ctx, sqlc.CreateOrgParams{Name: "pipeline-it-" + uuid.NewString()[:8], Region: "IN"})
	if err != nil {
		t.Fatalf("create org: %v", err)
	}
	h.user, err = h.queries.CreateUser(h.ctx, sqlc.CreateUserParams{
		OrgID: h.org.ID, Email: "orchestrator-it-" + uuid.NewString()[:8] + "@example.test",
		PasswordHash: "$argon2id$placeholder", Role: "doctor",
	})
	if err != nil {
		t.Fatalf("create user: %v", err)
	}
	grantedAt := pgtype.Timestamptz{Time: time.Now().UTC(), Valid: true}
	h.consent, err = h.queries.CreateConsentRecord(h.ctx, sqlc.CreateConsentRecordParams{
		OrgID: h.org.ID, SubjectRef: "it-subject-" + uuid.NewString(), ConsentType: "recording",
		GrantedAt: grantedAt, GrantedBy: h.user.ID,
	})
	if err != nil {
		t.Fatalf("create consent record: %v", err)
	}
	h.consultation, err = h.queries.CreateConsultation(h.ctx, sqlc.CreateConsultationParams{
		OrgID: h.org.ID, OwnerUserID: h.user.ID, ConsentRecordID: h.consent.ID,
		ConsentObtained: true, ConsentMethod: pgtype.Text{String: "verbal", Valid: true},
		ConsentRecordedAt: grantedAt, Language: "en",
	})
	if err != nil {
		t.Fatalf("create consultation: %v", err)
	}
	h.confirmAudio(t)
}

const testAudioSHA = "9f2a1c4e5d6b7a8c9e0f1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f"

func (h *harness) confirmAudio(t *testing.T) {
	t.Helper()
	key := fmt.Sprintf("test/consultations/%s/source/audio/%s.wav", h.consultation.ID, testAudioSHA)
	c, err := h.queries.UpdateConsultationAudio(h.ctx, sqlc.UpdateConsultationAudioParams{
		ID:             h.consultation.ID,
		SourceAudioUri: pgtype.Text{String: key, Valid: true},
		AudioSha256:    pgtype.Text{String: testAudioSHA, Valid: true},
		DurationSec:    pgtype.Float8{Float64: 600, Valid: true},
	})
	if err != nil {
		t.Fatalf("confirm audio: %v", err)
	}
	h.consultation = c
}

// runConfig interns an arm's RunConfig the way go-api's POST /jobs does.
func (h *harness) runConfig(t *testing.T, arm string, gotEnabled bool) *sqlc.RunConfig {
	t.Helper()
	cfg := &codev1.RunConfig{
		Arm: arm, SchemaVersion: 1,
		BaseModel: "qwen/qwen3.8-27b", JudgeModel: "openai/gpt-oss-20b",
		StructuralModel: "qwen/qwen3.6-27b", EmbedModel: "all-MiniLM-L6-v2",
		AsrBackend: "groq", AsrModel: "whisper-large-v3-turbo",
		GotEnabled: gotEnabled, NCandidates: 3, KIterations: 2,
		GraphContextEnabled: gotEnabled, KgEnabled: false, KgBackend: "none",
		Temperature: 0.2, TopP: 0.9, Seed: 42, RedactionEnabled: true,
	}
	raw, err := protojson.MarshalOptions{}.Marshal(cfg)
	if err != nil {
		t.Fatalf("marshal run config: %v", err)
	}
	rc, err := h.queries.CreateRunConfig(h.ctx, sqlc.CreateRunConfigParams{
		ContentHash: fmt.Sprintf("%s-%s", arm, uuid.NewString()),
		Arm:         arm, Config: raw, SchemaVersion: 1,
	})
	if err != nil {
		t.Fatalf("intern run config: %v", err)
	}
	return rc
}

// submitJob mirrors what POST /v1/consultations/{id}/jobs leaves behind:
// a jobs row in asr_queued, and nothing else.
func (h *harness) submitJob(t *testing.T, arm string, gotEnabled bool) *sqlc.Job {
	t.Helper()
	rc := h.runConfig(t, arm, gotEnabled)
	job, err := h.queries.CreateJob(h.ctx, sqlc.CreateJobParams{
		ConsultationID: h.consultation.ID, RunConfigID: rc.ID,
		TraceID: "trace-" + uuid.NewString()[:12],
	})
	if err != nil {
		t.Fatalf("create job: %v", err)
	}
	job, err = h.queries.UpdateJobState(h.ctx, sqlc.UpdateJobStateParams{ID: job.ID, State: string(StateASRQueued)})
	if err != nil {
		t.Fatalf("queue job: %v", err)
	}
	if _, err := h.queries.UpdateConsultationState(h.ctx, sqlc.UpdateConsultationStateParams{
		ID: h.consultation.ID, State: string(StateASRQueued),
	}); err != nil {
		t.Fatalf("queue consultation: %v", err)
	}
	return job
}

// newOrchestrator builds an orchestrator against this harness. Each one gets
// a distinct consumer name unless told otherwise, so the "restart" tests can
// choose whether the replacement process inherits its predecessor's PEL
// identity.
func (h *harness) newOrchestrator(consumerName string, artifacts ArtifactStatter, tweak ...func(*Config)) *Orchestrator {
	cfg := Config{
		ConsumerName:     consumerName,
		DispatchInterval: 200 * time.Millisecond,
		ReaperInterval:   200 * time.Millisecond,
		CancelInterval:   200 * time.Millisecond,
		// Zero-delay backoff: these tests assert *that* a retry is
		// scheduled and re-dispatched, not how long jitter waits. §2.5's
		// real 2s-120s window is asserted directly in backoff_test.go.
		Backoff: queue.Backoff{Base: time.Nanosecond, Max: time.Nanosecond},
	}
	for _, f := range tweak {
		f(&cfg)
	}
	return New(h.pool, h.q, artifacts, h.logger, cfg)
}

// --- worker simulation -------------------------------------------------

// readEnvelope consumes one message from a worker stream the way a Python
// worker would: XREADGROUP on the service's own consumer group.
func (h *harness) readEnvelope(t *testing.T, stream, consumer string, timeout time.Duration) (*codev1.StageEnvelope, string) {
	t.Helper()
	group, err := queue.GroupFor(stream)
	if err != nil {
		t.Fatalf("group for %s: %v", stream, err)
	}
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		res, err := h.rdb.XReadGroup(h.ctx, &redis.XReadGroupArgs{
			Group: group, Consumer: consumer, Streams: []string{stream, ">"},
			Count: 1, Block: 200 * time.Millisecond,
		}).Result()
		if err == redis.Nil {
			continue
		}
		if err != nil {
			t.Fatalf("xreadgroup %s: %v", stream, err)
		}
		for _, s := range res {
			for _, m := range s.Messages {
				payload, _ := m.Values[queue.FieldPayload].(string)
				env := &codev1.StageEnvelope{}
				if err := (protojson.UnmarshalOptions{DiscardUnknown: true}).Unmarshal([]byte(payload), env); err != nil {
					t.Fatalf("unmarshal envelope: %v", err)
				}
				return env, m.ID
			}
		}
	}
	t.Fatalf("no envelope appeared on %s within %s", stream, timeout)
	return nil, ""
}

// publishResult publishes a StageResult the way a worker does, echoing the
// envelope's identifiers so the orchestrator can resolve it.
func (h *harness) publishResult(t *testing.T, env *codev1.StageEnvelope, status codev1.Status, resultRef string, stageErr *codev1.Error, resumeAfter *time.Time) *codev1.StageResult {
	t.Helper()
	res := &codev1.StageResult{
		JobId: env.GetJobId(), ConsultationId: env.GetConsultationId(),
		Stage: env.GetStage(), Attempt: env.GetAttempt(),
		IdempotencyKey: env.GetIdempotencyKey(), TraceId: env.GetTraceId(),
		SchemaVersion: queue.SchemaVersion, Status: status, ResultRef: resultRef,
		Error:   stageErr,
		Metrics: &codev1.StageMetrics{TokensIn: 1200, TokensOut: 800, LlmCalls: 3, WallMs: 4200},
	}
	if resumeAfter != nil {
		res.ResumeAfter = timestamppb.New(*resumeAfter)
	}
	if _, err := h.q.PublishResultMessage(h.ctx, res); err != nil {
		t.Fatalf("publish result: %v", err)
	}
	return res
}

// artifactKeyFor builds a §3.3-conformant result_ref for a stage output.
func (h *harness) artifactKeyFor(job *sqlc.Job, stage, kind string) string {
	return fmt.Sprintf("test/consultations/%s/stages/%s/%s/%s.json", job.ConsultationID, stage, job.RunConfigID, kind)
}

// --- assertions --------------------------------------------------------

func (h *harness) job(t *testing.T, id uuid.UUID) *sqlc.Job {
	t.Helper()
	job, err := h.queries.GetJob(h.ctx, id)
	if err != nil {
		t.Fatalf("get job %s: %v", id, err)
	}
	return job
}

func (h *harness) stage(t *testing.T, jobID uuid.UUID, stage string) *sqlc.JobStage {
	t.Helper()
	row, err := h.queries.GetJobStageByJobAndStage(h.ctx, sqlc.GetJobStageByJobAndStageParams{JobID: jobID, Stage: stage})
	if err != nil {
		t.Fatalf("get stage %s of job %s: %v", stage, jobID, err)
	}
	return row
}

func (h *harness) stages(t *testing.T, jobID uuid.UUID) []*sqlc.JobStage {
	t.Helper()
	rows, err := h.queries.ListJobStagesByJob(h.ctx, jobID)
	if err != nil {
		t.Fatalf("list stages of job %s: %v", jobID, err)
	}
	return rows
}

// waitForJobState polls until the job reaches one of the wanted states, or
// fails with what it actually saw. Polling rather than sleeping keeps the
// tests fast and makes a failure report the real state.
func (h *harness) waitForJobState(t *testing.T, jobID uuid.UUID, timeout time.Duration, want ...State) *sqlc.Job {
	t.Helper()
	wanted := make(map[State]bool, len(want))
	for _, w := range want {
		wanted[w] = true
	}
	deadline := time.Now().Add(timeout)
	var last *sqlc.Job
	for time.Now().Before(deadline) {
		last = h.job(t, jobID)
		if wanted[State(last.State)] {
			return last
		}
		time.Sleep(25 * time.Millisecond)
	}
	t.Fatalf("job %s never reached %v within %s; it is in %q (current_stage=%v, attempt=%d, error=%s)",
		jobID, want, timeout, last.State, derefString(last.CurrentStage), last.Attempt, string(last.Error))
	return nil
}

// deadLetters reads stage.dlq without consuming it.
func (h *harness) deadLetters(t *testing.T) []*codev1.DeadLetter {
	t.Helper()
	dls, err := h.q.ReadDeadLetters(h.ctx, 50)
	if err != nil {
		t.Fatalf("read dead letters: %v", err)
	}
	return dls
}

func (h *harness) pendingCount(t *testing.T, stream string) int64 {
	t.Helper()
	group, err := queue.GroupFor(stream)
	if err != nil {
		t.Fatalf("group for %s: %v", stream, err)
	}
	n, err := h.q.PendingCount(h.ctx, stream, group)
	if err != nil {
		t.Fatalf("pending count for %s: %v", stream, err)
	}
	return n
}

// auditedStates returns the `after.state` of every audit_log row for one
// job, oldest first — the evidence for §4.1's "all transitions append to
// audit_log inside the same transaction that performs the transition".
//
// Tests assert on the *sequence*, not a fixed count: dispatch's
// mark-running transition is legitimately skipped when a worker returns its
// result before that write lands (see dispatchJob step 3), so the number of
// entries on a healthy happy path is timing-dependent while the ordered set
// of states the job passes through is not.
func (h *harness) auditedStates(t *testing.T, jobID uuid.UUID) []string {
	t.Helper()
	rows, err := h.pool.Query(h.ctx,
		`SELECT after->>'state' FROM audit_log
		 WHERE resource_type = 'job' AND resource_id = $1 AND action = 'job.transition'
		 ORDER BY at, id`, jobID.String())
	if err != nil {
		t.Fatalf("read audit entries: %v", err)
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var state *string
		if err := rows.Scan(&state); err != nil {
			t.Fatalf("scan audit entry: %v", err)
		}
		if state != nil {
			out = append(out, *state)
		}
	}
	if err := rows.Err(); err != nil {
		t.Fatalf("iterate audit entries: %v", err)
	}
	return out
}

func (h *harness) auditCountForJob(t *testing.T, jobID uuid.UUID) int {
	t.Helper()
	var n int
	err := h.pool.QueryRow(h.ctx,
		`SELECT count(*) FROM audit_log WHERE resource_type = 'job' AND resource_id = $1`,
		jobID.String()).Scan(&n)
	if err != nil {
		t.Fatalf("count audit entries: %v", err)
	}
	return n
}

// errorHistory decodes job_stages.error_history.
func errorHistory(t *testing.T, row *sqlc.JobStage) []map[string]any {
	t.Helper()
	var out []map[string]any
	if len(row.ErrorHistory) == 0 {
		return out
	}
	if err := json.Unmarshal(row.ErrorHistory, &out); err != nil {
		t.Fatalf("decode error_history: %v", err)
	}
	return out
}

// fakeStatter stands in for MinIO so artifact indexing is exercised without
// a third container. The orchestrator only ever reads metadata (§1.3), so
// this is the whole of its object-storage surface.
type fakeStatter struct {
	stats map[string]storage.ArtifactStat
	calls int
}

func (f *fakeStatter) StatArtifact(_ context.Context, key string) (storage.ArtifactStat, error) {
	f.calls++
	if s, ok := f.stats[key]; ok {
		return s, nil
	}
	return storage.ArtifactStat{
		SHA256: fmt.Sprintf("%064x", len(key)), Bytes: int64(len(key)), ContentType: "application/json",
	}, nil
}

func jobStageKey(jobID uuid.UUID, stage string) sqlc.GetJobStageByJobAndStageParams {
	return sqlc.GetJobStageByJobAndStageParams{JobID: jobID, Stage: stage}
}
