package queue

import (
	"time"

	codev1 "coda/go/internal/genproto/coda/v1"
)

// StagePolicy is the per-stage retry/timeout contract from
// docs/architecture.md §4.2 and §2.5. It is data, not code: the orchestrator
// reads these numbers, and nothing else decides how long a stage may run or
// how often it may be retried.
type StagePolicy struct {
	// SoftDeadline is stamped into StageEnvelope.deadline. A worker past it
	// aborts and returns RETRYABLE / DEADLINE_EXCEEDED rather than running
	// on (§4.2).
	SoftDeadline time.Duration
	// VisibilityTimeout is the XAUTOCLAIM min-idle threshold: how long an
	// entry may sit unacknowledged in the worker group's PEL before the
	// orchestrator's reaper assumes the worker died and reclaims it. Sized
	// at roughly 2x SoftDeadline throughout §4.2.
	VisibilityTimeout time.Duration
	// MaxAttempts is the ceiling from §2.5. Exceeding it routes to
	// stage.dlq. Quota parking does not count against it — see
	// pipeline.handleResult and §2.5's "quota parking is not a retry".
	MaxAttempts int32
}

// PolicyOptions are the two run-config dimensions §4.2 gives different
// numbers for. They come from the job's RunConfig, never from an env var,
// so a reported result's timeouts are reconstructible from the database
// alongside everything else (ADR-0012).
type PolicyOptions struct {
	// LocalASR is RunConfig.asr_backend == "faster_whisper_local". CPU
	// faster-whisper is ~4x the hosted Whisper budget.
	LocalASR bool
	// GoTArm is RunConfig.got_enabled. The GoT window is wide because
	// free-tier TPM throttling, not compute, dominates its wall-clock
	// (§4.2): a ~33K-token consultation cannot physically complete faster
	// than a few minutes of pure throttle.
	GoTArm bool
}

// PolicyOptionsFor derives PolicyOptions from a persisted RunConfig, so
// callers never hand-assemble them from loose booleans.
func PolicyOptionsFor(cfg *codev1.RunConfig) PolicyOptions {
	if cfg == nil {
		return PolicyOptions{}
	}
	return PolicyOptions{
		LocalASR: cfg.GetAsrBackend() == "faster_whisper_local",
		GoTArm:   cfg.GetGotEnabled(),
	}
}

// PolicyFor returns the policy for one stage under one run config. Every
// number here is §4.2's table, transcribed — if the table changes, this
// changes, and nowhere else does.
func PolicyFor(stage codev1.Stage, opts PolicyOptions) StagePolicy {
	switch stage {
	case codev1.Stage_STAGE_ASR:
		if opts.LocalASR {
			return StagePolicy{SoftDeadline: 20 * time.Minute, VisibilityTimeout: 40 * time.Minute, MaxAttempts: 3}
		}
		return StagePolicy{SoftDeadline: 5 * time.Minute, VisibilityTimeout: 10 * time.Minute, MaxAttempts: 3}
	case codev1.Stage_STAGE_REDACT:
		return StagePolicy{SoftDeadline: 60 * time.Second, VisibilityTimeout: 120 * time.Second, MaxAttempts: 2}
	case codev1.Stage_STAGE_NLP:
		if opts.GoTArm {
			return StagePolicy{SoftDeadline: 45 * time.Minute, VisibilityTimeout: 90 * time.Minute, MaxAttempts: 3}
		}
		return StagePolicy{SoftDeadline: 10 * time.Minute, VisibilityTimeout: 20 * time.Minute, MaxAttempts: 3}
	case codev1.Stage_STAGE_EXPORT:
		return StagePolicy{SoftDeadline: 60 * time.Second, VisibilityTimeout: 120 * time.Second, MaxAttempts: 2}
	default:
		// An unspecified stage should never reach dispatch. Give it the
		// tightest policy in the table so a bug fails fast and loudly
		// rather than occupying a 90-minute visibility window.
		return StagePolicy{SoftDeadline: 60 * time.Second, VisibilityTimeout: 120 * time.Second, MaxAttempts: 1}
	}
}

// HeartbeatInterval is the cadence workers emit StageHeartbeat at (§2.4).
const HeartbeatInterval = 15 * time.Second

// HeartbeatStallThreshold is §2.4's "a job whose heartbeats stopped more
// than 90s ago is considered stalled even if inside the visibility window".
// Six missed heartbeats, not one — a single missed tick is a slow worker,
// not a dead one, and reclaiming on that would re-dispatch healthy work.
const HeartbeatStallThreshold = 90 * time.Second

// StartupGrace bounds how long a dispatched stage may go without its *first*
// heartbeat before it counts as stalled. It has to exceed a cold Python
// worker's model-load time (pyannote + faster-whisper on CPU), which is why
// it is minutes rather than seconds.
const StartupGrace = 5 * time.Minute

// ReaperInterval is §2.4's "the orchestrator runs a reaper every 30s".
const ReaperInterval = 30 * time.Second
