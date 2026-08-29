package pipeline

import "github.com/prometheus/client_golang/prometheus"

// Metrics is the orchestrator's half of docs/architecture.md §8's key
// series: "stage duration histograms, attempt counts, DLQ depth,
// quota-park events".
//
// Quota-park events get their own counter rather than being folded into
// stage outcomes because on a free tier they are the operational number
// that actually predicts an eval sweep's wall-clock — a run that parks
// forty times a day is not failing, it is rate-limited, and those look
// identical in a generic error counter.
type Metrics struct {
	Transitions      *prometheus.CounterVec
	Dispatches       *prometheus.CounterVec
	StageOutcomes    *prometheus.CounterVec
	QuotaParks       *prometheus.CounterVec
	DeadLetters      *prometheus.CounterVec
	Reclaims         *prometheus.CounterVec
	Timeouts         *prometheus.CounterVec
	DuplicateResults *prometheus.CounterVec
	Cancellations    *prometheus.CounterVec

	// DLQDepth and JobsByState are gauges refreshed by the periodic jobs
	// in internal/tasks rather than written inline, since both are counts
	// of the world rather than events.
	DLQDepth    prometheus.Gauge
	JobsByState *prometheus.GaugeVec
}

// NewMetrics constructs the collectors. They are not registered here —
// cmd/orchestrator registers them, so a test can build an Orchestrator
// without colliding on the default registry.
func NewMetrics() *Metrics {
	return &Metrics{
		Transitions: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "coda_pipeline_transitions_total",
			Help: "Pipeline state transitions, by the state transitioned into.",
		}, []string{"state"}),
		Dispatches: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "coda_pipeline_dispatches_total",
			Help: "Stage envelopes published to a worker stream, by stage.",
		}, []string{"stage"}),
		StageOutcomes: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "coda_pipeline_stage_outcomes_total",
			Help: "Stage results applied, by stage and resulting job_stages status.",
		}, []string{"stage", "status"}),
		QuotaParks: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "coda_pipeline_quota_parks_total",
			Help: "Jobs parked on QUOTA_EXHAUSTED without consuming an attempt, by stage.",
		}, []string{"stage"}),
		DeadLetters: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "coda_pipeline_dead_letters_total",
			Help: "Messages routed to stage.dlq, by stage.",
		}, []string{"stage"}),
		Reclaims: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "coda_pipeline_reclaimed_messages_total",
			Help: "Messages reclaimed by XAUTOCLAIM from a stalled worker, by stage.",
		}, []string{"stage"}),
		Timeouts: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "coda_pipeline_stage_timeouts_total",
			Help: "Stages found stalled past their soft deadline or heartbeat gap, by stage.",
		}, []string{"stage"}),
		DuplicateResults: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "coda_pipeline_duplicate_results_total",
			Help: "Stage results absorbed as duplicates under at-least-once delivery, by stage.",
		}, []string{"stage"}),
		Cancellations: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "coda_pipeline_cancellations_total",
			Help: "Jobs halted, by reason (cancel_requested, consent_revoked, consultation_erased).",
		}, []string{"reason"}),
		DLQDepth: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "coda_pipeline_dlq_depth",
			Help: "Current entry count of the stage.dlq stream.",
		}),
		JobsByState: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Name: "coda_pipeline_jobs",
			Help: "Current job count, by pipeline state.",
		}, []string{"state"}),
	}
}

// Collectors lists everything for registration.
func (m *Metrics) Collectors() []prometheus.Collector {
	return []prometheus.Collector{
		m.Transitions, m.Dispatches, m.StageOutcomes, m.QuotaParks, m.DeadLetters,
		m.Reclaims, m.Timeouts, m.DuplicateResults, m.Cancellations,
		m.DLQDepth, m.JobsByState,
	}
}

func (m *Metrics) RecordTransition(state string) { m.Transitions.WithLabelValues(state).Inc() }
func (m *Metrics) RecordDispatch(stage string)   { m.Dispatches.WithLabelValues(stage).Inc() }
func (m *Metrics) RecordQuotaPark(stage string)  { m.QuotaParks.WithLabelValues(stage).Inc() }
func (m *Metrics) RecordDeadLetter(stage string) { m.DeadLetters.WithLabelValues(stage).Inc() }
func (m *Metrics) RecordReclaim(stage string)    { m.Reclaims.WithLabelValues(stage).Inc() }
func (m *Metrics) RecordTimeout(stage string)    { m.Timeouts.WithLabelValues(stage).Inc() }
func (m *Metrics) RecordCancellation(reason string) {
	m.Cancellations.WithLabelValues(reason).Inc()
}
func (m *Metrics) RecordDuplicateResult(stage string) {
	m.DuplicateResults.WithLabelValues(stage).Inc()
}
func (m *Metrics) RecordStageOutcome(stage, status string) {
	m.StageOutcomes.WithLabelValues(stage, status).Inc()
}
