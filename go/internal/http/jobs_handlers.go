package http

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"google.golang.org/protobuf/encoding/protojson"

	"coda/go/internal/audit"
	"coda/go/internal/auth"
	"coda/go/internal/db/sqlc"
	codev1 "coda/go/internal/genproto/coda/v1"
	"coda/go/internal/queue"
)

// knownArms mirrors the ablation matrix (docs/architecture.md §6.2).
// frontier_ref's base_model deliberately differs from the rest — an "upper
// reference row", not part of the controlled ablation — reusing
// gpt-oss-120b (already the reference-label model per decision #3/ADR-0013)
// since claude_context.md doesn't pin a separate frontier model name; this
// is a judgment call, flagged here rather than picked silently.
var knownArms = map[string]func() *codev1.RunConfig{
	"baseline":       func() *codev1.RunConfig { return baseRunConfig("baseline", false, 1, 0, false, false) },
	"got_k1":         func() *codev1.RunConfig { return baseRunConfig("got_k1", true, 3, 1, true, false) },
	"got_k2":         func() *codev1.RunConfig { return baseRunConfig("got_k2", true, 3, 2, true, false) },
	"got_k2_nograph": func() *codev1.RunConfig { return baseRunConfig("got_k2_nograph", true, 3, 2, false, false) },
	"got_k2_kg":      func() *codev1.RunConfig { return baseRunConfig("got_k2_kg", true, 3, 2, true, true) },
	"frontier_ref": func() *codev1.RunConfig {
		cfg := baseRunConfig("frontier_ref", false, 1, 0, false, false)
		cfg.BaseModel = "openai/gpt-oss-120b"
		return cfg
	},
}

const defaultArm = "baseline"

// baseRunConfig fills in the LLM model assignment from claude_context.md
// §4 and the GoT knobs for one ablation-matrix row (§6.2). Scorer weights,
// temperature/top_p/seed are placeholder defaults pending Phase 6 tuning —
// they are still recorded so content_hash is fully reproducible from this
// object alone (ADR-0012), not left implicit.
func baseRunConfig(arm string, gotEnabled bool, nCandidates, kIterations uint32, graphContext, kgEnabled bool) *codev1.RunConfig {
	kgBackend := "none"
	if kgEnabled {
		kgBackend = "scispacy_mesh" // primary backend, decision #9/ADR-0010
	}
	return &codev1.RunConfig{
		Arm:                 arm,
		SchemaVersion:       1,
		BaseModel:           "llama-3.3-70b-versatile",
		JudgeModel:          "openai/gpt-oss-20b",
		StructuralModel:     "llama-3.1-8b-instant",
		EmbedModel:          "all-MiniLM-L6-v2",
		AsrBackend:          "groq",
		AsrModel:            "whisper-large-v3-turbo",
		GotEnabled:          gotEnabled,
		NCandidates:         nCandidates,
		KIterations:         kIterations,
		GraphContextEnabled: graphContext,
		KgEnabled:           kgEnabled,
		KgBackend:           kgBackend,
		ScorerWeights:       &codev1.ScorerWeights{Relevance: 0.5, Consistency: 0.3, Redundancy: 0.2},
		Temperature:         0.2,
		TopP:                0.9,
		Seed:                42,
		PromptSetHash:       "", // no prompt files exist yet (Phase 4/6 not started) — honestly empty, not fabricated
		RedactionEnabled:    true,
	}
}

// runConfigContentHash mirrors architecture.md §6.1:
// content_hash = sha256(canonical_json(RunConfig)). protojson is the
// project's one canonical JSON encoding for proto messages (ADR-0004); the
// same bytes are what gets persisted into run_configs.config.
func runConfigContentHash(cfg *codev1.RunConfig) (hash string, canonicalJSON []byte, err error) {
	b, err := protojson.MarshalOptions{}.Marshal(cfg)
	if err != nil {
		return "", nil, err
	}
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:]), b, nil
}

type createJobRequest struct {
	Arm string `json:"arm"`
}

type createJobResponse struct {
	JobID          uuid.UUID `json:"job_id"`
	ConsultationID uuid.UUID `json:"consultation_id"`
	RunConfigID    uuid.UUID `json:"run_config_id"`
	Arm            string    `json:"arm"`
	State          string    `json:"state"`
}

// CreateJob enqueues one pipeline run for a consultation. It never blocks
// on processing (docs/architecture.md §1.2: go-api must not run ML or
// block on pipeline work) — it durably records the job row, which is the
// real source of truth, and calls the queue.Enqueuer stub as a best-effort
// dispatch signal (see internal/queue/doc.go: go-orchestrator, the actual
// consumer, doesn't exist yet).
func (h *Handlers) CreateJob(w http.ResponseWriter, r *http.Request) {
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
		h.logger.ErrorContext(r.Context(), "create job: get consultation", "error", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}
	if !auth.RequireSameOrg(w, claims, c.OrgID) {
		return
	}
	if c.ErasedAt.Valid {
		writeError(w, http.StatusUnprocessableEntity, "consultation has been erased")
		return
	}
	if !c.SourceAudioUri.Valid {
		writeError(w, http.StatusUnprocessableEntity, "audio has not been uploaded yet — confirm an upload before submitting a job")
		return
	}

	var req createJobRequest
	if r.ContentLength != 0 {
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			writeError(w, http.StatusBadRequest, "invalid request body")
			return
		}
	}
	arm := req.Arm
	if arm == "" {
		arm = defaultArm
	}
	build, ok := knownArms[arm]
	if !ok {
		writeError(w, http.StatusUnprocessableEntity, fmt.Sprintf(
			"arm must be one of baseline, got_k1, got_k2, got_k2_nograph, got_k2_kg, frontier_ref (got %q)", arm))
		return
	}
	cfg := build()

	contentHash, canonicalJSON, err := runConfigContentHash(cfg)
	if err != nil {
		h.logger.ErrorContext(r.Context(), "create job: marshal run config", "error", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}
	runConfig, err := h.queries.CreateRunConfig(r.Context(), sqlc.CreateRunConfigParams{
		ContentHash:   contentHash,
		Arm:           cfg.Arm,
		Config:        canonicalJSON,
		SchemaVersion: int32(cfg.SchemaVersion),
	})
	if err != nil {
		h.logger.ErrorContext(r.Context(), "create job: intern run config", "error", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}

	traceID := middleware.GetReqID(r.Context())
	job, err := h.queries.CreateJob(r.Context(), sqlc.CreateJobParams{
		ConsultationID: c.ID,
		RunConfigID:    runConfig.ID,
		TraceID:        traceID,
	})
	if err != nil {
		h.logger.ErrorContext(r.Context(), "create job: create job row", "error", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}

	// UPLOADED -> ASR_QUEUED (docs/architecture.md §4.1). consultations.state
	// tracks whichever job feeds the doctor-facing review UI — the job just
	// created for a consultation with no other in-flight job is that job.
	if _, err := h.queries.UpdateConsultationState(r.Context(), sqlc.UpdateConsultationStateParams{ID: c.ID, State: "asr_queued"}); err != nil {
		h.logger.ErrorContext(r.Context(), "create job: advance consultation state", "error", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}
	if _, err := h.queries.UpdateJobState(r.Context(), sqlc.UpdateJobStateParams{
		ID:    job.ID,
		State: "asr_queued",
	}); err != nil {
		h.logger.ErrorContext(r.Context(), "create job: advance job state", "error", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}

	if h.enqueuer != nil {
		if err := h.enqueuer.Enqueue(r.Context(), queue.JobRequest{
			JobID: job.ID, ConsultationID: c.ID, RunConfigID: runConfig.ID,
		}); err != nil {
			// Best-effort dispatch signal only — the jobs row above is the
			// durable record. Log and still return 202: this must never
			// block on processing.
			h.logger.ErrorContext(r.Context(), "create job: enqueue signal failed", "error", err)
		}
	}

	audit.SetResource(r.Context(), "job", job.ID.String())
	audit.SetAction(r.Context(), "job.create")

	writeJSON(w, http.StatusAccepted, createJobResponse{
		JobID: job.ID, ConsultationID: c.ID, RunConfigID: runConfig.ID, Arm: cfg.Arm, State: "asr_queued",
	})
}

type jobStageResponse struct {
	Stage      string          `json:"stage"`
	Status     string          `json:"status"`
	Attempt    int32           `json:"attempt"`
	StartedAt  *time.Time      `json:"started_at,omitempty"`
	FinishedAt *time.Time      `json:"finished_at,omitempty"`
	ResultRef  string          `json:"result_ref,omitempty"`
	Metrics    json.RawMessage `json:"metrics,omitempty"`

	// Worker-reported progress within the stage, from the StageHeartbeat
	// go-orchestrator lands on job_stages every 15s
	// (docs/architecture.md §2.4). go-api reads it from Postgres and never
	// touches the stage.progress stream — §1.2 forbids that, and
	// claude_context.md decision #35 keeps this endpoint's read path
	// unchanged.
	PercentComplete float32    `json:"percent_complete"`
	Step            string     `json:"step,omitempty"`
	HeartbeatAt     *time.Time `json:"heartbeat_at,omitempty"`
}

type jobResponse struct {
	ID              uuid.UUID          `json:"id"`
	ConsultationID  uuid.UUID          `json:"consultation_id"`
	RunConfigID     uuid.UUID          `json:"run_config_id"`
	State           string             `json:"state"`
	CurrentStage    string             `json:"current_stage,omitempty"`
	Attempt         int32              `json:"attempt"`
	ResumeAfter     *time.Time         `json:"resume_after,omitempty"`
	Error           json.RawMessage    `json:"error,omitempty"`
	ProgressPercent int                `json:"progress_percent"`
	Stages          []jobStageResponse `json:"stages"`
}

// pipelineStages is the fixed stage sequence a job passes through
// (proto/coda/v1/common.proto Stage enum), used only to compute a rough
// progress percentage — succeeded-stage-count / len(pipelineStages).
var pipelineStages = []string{"asr", "redact", "nlp", "export"}

func loadJobForOrg(ctx context.Context, h *Handlers, jobID uuid.UUID, orgID uuid.UUID) (*sqlc.Job, error) {
	job, err := h.queries.GetJob(ctx, jobID)
	if err != nil {
		return nil, err
	}
	c, err := h.queries.GetConsultation(ctx, job.ConsultationID)
	if err != nil {
		return nil, err
	}
	if c.OrgID != orgID {
		return nil, pgx.ErrNoRows // cross-org: reported as not-found, matching RequireSameOrg's posture elsewhere
	}
	return job, nil
}

// GetJob returns per-stage state, a rough progress percentage, timings, and
// error detail for one job.
func (h *Handlers) GetJob(w http.ResponseWriter, r *http.Request) {
	claims, ok := auth.ClaimsFromContext(r.Context())
	if !ok {
		writeError(w, http.StatusUnauthorized, "authentication required")
		return
	}
	id, err := uuid.Parse(chi.URLParam(r, "id"))
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid job id")
		return
	}

	job, err := loadJobForOrg(r.Context(), h, id, claims.OrgID)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			writeError(w, http.StatusNotFound, "not found")
			return
		}
		h.logger.ErrorContext(r.Context(), "get job", "error", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}

	stages, err := h.queries.ListJobStagesByJob(r.Context(), job.ID)
	if err != nil {
		h.logger.ErrorContext(r.Context(), "get job: list stages", "error", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}

	audit.SetResource(r.Context(), "job", job.ID.String())
	audit.SetAction(r.Context(), "job.read")

	writeJSON(w, http.StatusOK, buildJobResponse(job, stages))
}

func buildJobResponse(job *sqlc.Job, stages []*sqlc.JobStage) jobResponse {
	resp := jobResponse{
		ID: job.ID, ConsultationID: job.ConsultationID, RunConfigID: job.RunConfigID,
		State: job.State, Attempt: job.Attempt, Error: json.RawMessage(job.Error),
	}
	if job.CurrentStage != nil {
		resp.CurrentStage = *job.CurrentStage
	}
	if job.ResumeAfter.Valid {
		t := job.ResumeAfter.Time
		resp.ResumeAfter = &t
	}

	succeeded := 0
	resp.Stages = make([]jobStageResponse, 0, len(stages))
	for _, s := range stages {
		sr := jobStageResponse{
			Stage: s.Stage, Status: s.Status, Attempt: s.Attempt,
			Metrics: json.RawMessage(s.Metrics), PercentComplete: s.PercentComplete,
		}
		if s.Step.Valid {
			sr.Step = s.Step.String
		}
		if s.HeartbeatAt.Valid {
			t := s.HeartbeatAt.Time
			sr.HeartbeatAt = &t
		}
		if s.StartedAt.Valid {
			t := s.StartedAt.Time
			sr.StartedAt = &t
		}
		if s.FinishedAt.Valid {
			t := s.FinishedAt.Time
			sr.FinishedAt = &t
		}
		if s.ResultRef.Valid {
			sr.ResultRef = s.ResultRef.String
		}
		if s.Status == "succeeded" {
			succeeded++
		}
		resp.Stages = append(resp.Stages, sr)
	}
	resp.ProgressPercent = succeeded * 100 / len(pipelineStages)
	return resp
}

var terminalJobStates = map[string]bool{
	"approved": true, "exported": true, "failed": true, "dead_lettered": true, "cancelled": true,
}

const (
	sseHeartbeatInterval = 15 * time.Second // mirrors the worker heartbeat cadence, architecture.md §2.4
	ssePollInterval      = 2 * time.Second
	sseMaxDuration       = 30 * time.Minute
)

// JobEvents streams stage transitions and progress heartbeats over SSE.
// go-orchestrator (the real event source, docs/architecture.md §2.4's
// StageHeartbeat stream) doesn't exist yet, so this polls job/job_stages
// state on an interval and emits an event whenever it changes — the same
// contract a future pub/sub-backed implementation would present to
// clients, just backed by polling until the orchestrator exists to push.
func (h *Handlers) JobEvents(w http.ResponseWriter, r *http.Request) {
	claims, ok := auth.ClaimsFromContext(r.Context())
	if !ok {
		writeError(w, http.StatusUnauthorized, "authentication required")
		return
	}
	id, err := uuid.Parse(chi.URLParam(r, "id"))
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid job id")
		return
	}
	job, err := loadJobForOrg(r.Context(), h, id, claims.OrgID)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			writeError(w, http.StatusNotFound, "not found")
			return
		}
		h.logger.ErrorContext(r.Context(), "job events", "error", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}

	flusher, ok := w.(http.Flusher)
	if !ok {
		writeError(w, http.StatusInternalServerError, "streaming not supported")
		return
	}

	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.WriteHeader(http.StatusOK)

	ctx, cancel := context.WithTimeout(r.Context(), sseMaxDuration)
	defer cancel()

	pollTicker := time.NewTicker(ssePollInterval)
	defer pollTicker.Stop()
	heartbeatTicker := time.NewTicker(sseHeartbeatInterval)
	defer heartbeatTicker.Stop()

	var lastSignature string
	emit := func() (done bool) {
		stages, err := h.queries.ListJobStagesByJob(ctx, job.ID)
		if err != nil {
			return false
		}
		current, err := h.queries.GetJob(ctx, job.ID)
		if err != nil {
			return false
		}
		resp := buildJobResponse(current, stages)
		// The signature includes each stage's heartbeat progress, not just
		// the job-level state: without it a long stage (the GoT NLP arm
		// runs up to 45 minutes) would emit one frame at dispatch and then
		// nothing until it finished, which is exactly the case a progress
		// stream exists for.
		sig := fmt.Sprintf("%s|%s|%d|%d", current.State, resp.CurrentStage, current.Attempt, resp.ProgressPercent)
		for _, st := range resp.Stages {
			sig += fmt.Sprintf("|%s:%s:%.1f:%s", st.Stage, st.Status, st.PercentComplete, st.Step)
		}
		if sig == lastSignature {
			return terminalJobStates[current.State]
		}
		lastSignature = sig
		b, err := json.Marshal(resp)
		if err != nil {
			return false
		}
		if _, err := fmt.Fprintf(w, "event: stage\ndata: %s\n\n", b); err != nil {
			return true // client gone — stop the stream rather than looping on write errors
		}
		flusher.Flush()
		return terminalJobStates[current.State]
	}

	if emit() {
		return
	}
	for {
		select {
		case <-ctx.Done():
			return
		case <-pollTicker.C:
			if emit() {
				return
			}
		case <-heartbeatTicker.C:
			if _, err := fmt.Fprint(w, ": heartbeat\n\n"); err != nil {
				return
			}
			flusher.Flush()
		}
	}
}

type consultationResultResponse struct {
	ConsultationID uuid.UUID         `json:"consultation_id"`
	JobID          uuid.UUID         `json:"job_id"`
	RunConfigID    uuid.UUID         `json:"run_config_id"`
	State          string            `json:"state"`
	Transcript     *transcriptResult `json:"transcript,omitempty"`
	Summary        *summaryResult    `json:"summary,omitempty"`
	ClinicalNote   json.RawMessage   `json:"clinical_note,omitempty"`
	Artifacts      []artifactLink    `json:"artifacts"`
}

type transcriptResult struct {
	URI        string  `json:"uri"`
	AsrBackend string  `json:"asr_backend"`
	AsrModel   string  `json:"asr_model"`
	Language   string  `json:"language"`
	Wer        float64 `json:"wer,omitempty"`
	Der        float64 `json:"der,omitempty"`
}

type summaryResult struct {
	Text string `json:"text"`
}

type artifactLink struct {
	Kind string `json:"kind"`
	URI  string `json:"uri"`
}

var resultReadyStates = map[string]bool{
	"awaiting_review": true, "under_review": true, "approved": true, "exported": true,
}

// GetConsultationResult assembles the transcript, extraction/note, summary,
// and artifact links for a consultation's most recent job. It returns 409
// while the pipeline is still running (docs/architecture.md §7.5 posture:
// nothing downstream is treated as ready before the model has actually
// produced it) rather than a partial or empty 200.
func (h *Handlers) GetConsultationResult(w http.ResponseWriter, r *http.Request) {
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
		h.logger.ErrorContext(r.Context(), "get result: get consultation", "error", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}
	if !auth.RequireSameOrg(w, claims, c.OrgID) {
		return
	}

	jobs, err := h.queries.ListJobsByConsultation(r.Context(), c.ID)
	if err != nil {
		h.logger.ErrorContext(r.Context(), "get result: list jobs", "error", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}
	if len(jobs) == 0 {
		writeError(w, http.StatusConflict, "processing has not started for this consultation")
		return
	}
	job := jobs[len(jobs)-1] // most recently created (ListJobsByConsultation orders by created_at ascending)

	if !resultReadyStates[job.State] {
		writeError(w, http.StatusConflict, fmt.Sprintf("still processing (job state: %s)", job.State))
		return
	}

	note, err := h.queries.GetClinicalNoteForConsultationAndRunConfig(r.Context(), sqlc.GetClinicalNoteForConsultationAndRunConfigParams{
		ConsultationID: c.ID, RunConfigID: job.RunConfigID,
	})
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			writeError(w, http.StatusConflict, "still processing — no clinical note persisted yet")
			return
		}
		h.logger.ErrorContext(r.Context(), "get result: get clinical note", "error", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}

	resp := consultationResultResponse{
		ConsultationID: c.ID, JobID: job.ID, RunConfigID: job.RunConfigID, State: job.State,
		ClinicalNote: json.RawMessage(note.Note),
	}

	if t, err := h.queries.GetTranscriptForConsultationAndRunConfig(r.Context(), sqlc.GetTranscriptForConsultationAndRunConfigParams{
		ConsultationID: c.ID, RunConfigID: job.RunConfigID,
	}); err == nil {
		tr := &transcriptResult{URI: t.Uri, AsrBackend: t.AsrBackend, AsrModel: t.AsrModel, Language: t.Language}
		if t.Wer.Valid {
			tr.Wer = t.Wer.Float64
		}
		if t.Der.Valid {
			tr.Der = t.Der.Float64
		}
		resp.Transcript = tr
	} else if !errors.Is(err, pgx.ErrNoRows) {
		h.logger.ErrorContext(r.Context(), "get result: get transcript", "error", err)
	}

	if s, err := h.queries.GetSummaryForConsultationAndRunConfig(r.Context(), sqlc.GetSummaryForConsultationAndRunConfigParams{
		ConsultationID: c.ID, RunConfigID: job.RunConfigID,
	}); err == nil {
		resp.Summary = &summaryResult{Text: s.Text}
	} else if !errors.Is(err, pgx.ErrNoRows) {
		h.logger.ErrorContext(r.Context(), "get result: get summary", "error", err)
	}

	artifacts, err := h.queries.ListArtifactsByConsultation(r.Context(), sqlc.ListArtifactsByConsultationParams{
		ConsultationID: c.ID, RunConfigID: pgUUIDValid(job.RunConfigID),
	})
	if err != nil {
		h.logger.ErrorContext(r.Context(), "get result: list artifacts", "error", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}
	resp.Artifacts = make([]artifactLink, 0, len(artifacts))
	for _, a := range artifacts {
		resp.Artifacts = append(resp.Artifacts, artifactLink{Kind: a.Kind, URI: a.Uri})
	}

	audit.SetResource(r.Context(), "consultation", c.ID.String())
	audit.SetAction(r.Context(), "consultation.result.read")
	audit.MarkClinicalRead(r.Context())

	writeJSON(w, http.StatusOK, resp)
}

type cancelConsultationResponse struct {
	ConsultationID  uuid.UUID `json:"consultation_id"`
	CancelRequested bool      `json:"cancel_requested"`
	State           string    `json:"state"`
}

// CancelConsultation is go-api's half of cooperative cancellation
// (docs/architecture.md §4.4). It sets consultations.cancel_requested and
// writes an audit entry — and does nothing else.
//
// It deliberately does not transition any state: that is go-orchestrator's
// exclusive authority (ADR-0006), and a second component moving a job to
// CANCELLED is exactly the "two writers to one state machine" problem the
// orchestrator-owned design exists to avoid. The orchestrator's
// cancellation sweep observes the flag, stops dispatching, and moves the
// job; the Python workers observe it at their own checkpoints and abort
// (§4.4). In-flight provider calls are not interrupted, so tokens already
// spent stay accounted.
//
// The flag lives on consultations rather than jobs, per §5.2's table (see
// claude_context.md's note on §4.4's prose disagreeing with it), so
// cancelling a consultation cancels every ablation arm running over it —
// which is what a user clicking "cancel" means.
//
// Idempotent: cancelling an already-cancelled consultation is a 200, not a
// conflict.
func (h *Handlers) CancelConsultation(w http.ResponseWriter, r *http.Request) {
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
		h.logger.ErrorContext(r.Context(), "cancel consultation: get", "error", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}
	if !auth.RequireSameOrg(w, claims, c.OrgID) {
		return
	}

	updated, err := h.queries.RequestConsultationCancel(r.Context(), c.ID)
	if err != nil {
		h.logger.ErrorContext(r.Context(), "cancel consultation: set flag", "error", err)
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}

	audit.SetResource(r.Context(), "consultation", c.ID.String())
	audit.SetAction(r.Context(), "consultation.cancel")

	writeJSON(w, http.StatusOK, cancelConsultationResponse{
		ConsultationID: updated.ID, CancelRequested: updated.CancelRequested, State: updated.State,
	})
}
