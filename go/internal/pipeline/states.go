package pipeline

import (
	codev1 "coda/go/internal/genproto/coda/v1"
	"coda/go/internal/queue"
)

// State is a pipeline_state domain value (go/migrations/000010_domains).
// The string form is the column value; docs/architecture.md §4.1 writes
// them in upper case.
type State string

const (
	StateCreated         State = "created"
	StateConsentRecorded State = "consent_recorded"
	StateUploaded        State = "uploaded"
	StateASRQueued       State = "asr_queued"
	StateASRRunning      State = "asr_running"
	StateASRDone         State = "asr_done"
	StateRedactRunning   State = "redact_running"
	StateRedacted        State = "redacted"
	StateNLPQueued       State = "nlp_queued"
	StateNLPRunning      State = "nlp_running"
	StateNLPDone         State = "nlp_done"
	StateAwaitingReview  State = "awaiting_review"
	StateUnderReview     State = "under_review"
	StateApproved        State = "approved"
	StateExported        State = "exported"

	// Terminal off-ramps (§4.1).
	//
	// FAILED and DEAD_LETTERED are both terminal failures, and §4.1 lists
	// them separately without saying how they differ in practice. Resolved
	// here, consistently, by what an operator can *do* about each:
	//
	//   DEAD_LETTERED — a DeadLetter carrying the full envelope and failure
	//                   history reached stage.dlq. Replayable: §2.5's
	//                   operator re-XADD with attempt = 1 is available.
	//   FAILED        — a terminal condition with no replayable message,
	//                   either because there was never an envelope (an
	//                   unloadable run config, an unroutable stage) or
	//                   because publishing the DeadLetter itself failed.
	//
	// The practical consequence: a DEAD_LETTERED job can be recovered from
	// Redis; a FAILED one has to be re-submitted. Recorded because reading
	// §4.1 alone leaves the two indistinguishable.
	StateFailed       State = "failed"
	StateDeadLettered State = "dead_lettered"
	StateCancelled    State = "cancelled"
)

// terminalStates are states the orchestrator will never transition out of.
// APPROVED and EXPORTED are included because they are downstream of the
// human review gate (§7.5) and belong to go-api, not to this state machine.
var terminalStates = map[State]bool{
	StateApproved:     true,
	StateExported:     true,
	StateFailed:       true,
	StateDeadLettered: true,
	StateCancelled:    true,
}

// IsTerminal reports whether the orchestrator is done with this job.
func IsTerminal(s State) bool { return terminalStates[s] }

// TerminalStateNames is the list form, for the ListCancelRequestedJobs
// query's `NOT (state = ANY(...))` filter.
func TerminalStateNames() []string {
	out := make([]string, 0, len(terminalStates))
	for s := range terminalStates {
		out = append(out, string(s))
	}
	return out
}

// step is one node of the automatic pipeline: the state a job rests in
// before the stage runs, the state it occupies while running, and the state
// it reaches on success.
type step struct {
	Stage   codev1.Stage
	Rest    State
	Running State
	Done    State
}

// autoPipeline is §4.1's happy path, minus everything downstream of the
// review gate. Three stages run automatically; the fourth (export) is not
// here on purpose — §4.1 gives APPROVED no automatic inbound transition and
// §7.5 makes the doctor's approval the only edge into it, so an orchestrator
// that could dispatch export would be able to route around the human.
//
// The redact step between asr and nlp is mandatory and unskippable (§4.1,
// §7.3, ADR-0014): it is the last point before transcript text reaches a
// third-party LLM. It is expressed as a table entry rather than a
// conditional precisely so that "skip redaction" is not a reachable state
// in this code — the only way to skip it would be to delete a row here.
var autoPipeline = []step{
	{Stage: codev1.Stage_STAGE_ASR, Rest: StateASRQueued, Running: StateASRRunning, Done: StateASRDone},
	{Stage: codev1.Stage_STAGE_REDACT, Rest: StateASRDone, Running: StateRedactRunning, Done: StateRedacted},
	{Stage: codev1.Stage_STAGE_NLP, Rest: StateNLPQueued, Running: StateNLPRunning, Done: StateNLPDone},
}

// restStateAliases maps additional states a stage can be dispatched from
// onto that stage's canonical rest state.
//
// Two aliases exist, for different reasons:
//
//   - UPLOADED -> asr. A job created by POST /jobs is written straight to
//     ASR_QUEUED, but a consultation that reached UPLOADED before a job
//     existed must still be dispatchable.
//   - REDACTED -> nlp. §4.1's diagram has REDACTED and NLP_QUEUED as
//     distinct nodes with an edge between them; both mean "ready for NLP",
//     so both dispatch it.
//
// REDACT has no *_QUEUED state of its own anywhere in §4.1, which is why
// ASR_DONE serves as both "asr finished" and "redact is next" — and why a
// quota-parked redact stage parks in ASR_DONE (see parkState).
var restStateAliases = map[State]codev1.Stage{
	StateUploaded:  codev1.Stage_STAGE_ASR,
	StateASRQueued: codev1.Stage_STAGE_ASR,
	StateASRDone:   codev1.Stage_STAGE_REDACT,
	StateRedacted:  codev1.Stage_STAGE_NLP,
	StateNLPQueued: codev1.Stage_STAGE_NLP,
}

// DispatchableStateNames lists every state the dispatch scan looks for.
func DispatchableStateNames() []string {
	out := make([]string, 0, len(restStateAliases))
	for s := range restStateAliases {
		out = append(out, string(s))
	}
	return out
}

// RunningStateNames lists the states a job occupies while a stage is in
// flight — the input to stale-job reaping.
func RunningStateNames() []string {
	return []string{string(StateASRRunning), string(StateRedactRunning), string(StateNLPRunning)}
}

// stageForState returns the stage to dispatch from a rest state.
func stageForState(s State) (codev1.Stage, bool) {
	stage, ok := restStateAliases[s]
	return stage, ok
}

// stepFor looks up a stage's row in the pipeline table.
func stepFor(stage codev1.Stage) (step, bool) {
	for _, st := range autoPipeline {
		if st.Stage == stage {
			return st, true
		}
	}
	return step{}, false
}

// nextAfter returns the state a job moves to once `stage` succeeds. The
// final stage's success leads to AWAITING_REVIEW, where the pipeline stops
// and waits for a human (§4.1: NLP_DONE writes the note with status =
// 'draft'; nothing is exportable yet).
func nextAfter(stage codev1.Stage) (State, bool) {
	for i, st := range autoPipeline {
		if st.Stage != stage {
			continue
		}
		if i == len(autoPipeline)-1 {
			return StateAwaitingReview, true
		}
		return st.Done, true
	}
	return "", false
}

// parkState is where a job waits when a stage must be retried or is
// quota-parked. §2.5 says "*_QUEUED"; redact has no such state in §4.1, so
// it parks in ASR_DONE — the same state it is dispatched from, which is
// what makes the dispatch scan pick it up again when resume_after elapses.
func parkState(stage codev1.Stage) State {
	st, ok := stepFor(stage)
	if !ok {
		return StateFailed
	}
	return st.Rest
}

// resumeState computes where a job should restart from, given the stages it
// has already completed — "resume from the last successful stage" (§4.3).
//
// It walks the pipeline in order and stops at the first stage without a
// succeeded row. Completed stages are never recomputed: their artifacts are
// already durable, and re-running one would re-pay its token cost against a
// daily quota for an output that already exists (§4.3, ADR-0015).
//
// A job with every stage succeeded resumes at AWAITING_REVIEW, not at a
// dispatch — there is nothing left to run.
func resumeState(succeeded map[codev1.Stage]bool) State {
	for _, st := range autoPipeline {
		if !succeeded[st.Stage] {
			return st.Rest
		}
	}
	return StateAwaitingReview
}

// stageStatusFor maps a worker's Status classification onto the
// job_stages.status CHECK values (go/migrations/000013).
func stageStatusFor(s codev1.Status) string {
	switch s {
	case codev1.Status_STATUS_OK:
		return "succeeded"
	case codev1.Status_STATUS_QUOTA_EXHAUSTED:
		return "quota_exhausted"
	case codev1.Status_STATUS_CANCELLED:
		return "cancelled"
	default:
		return "failed"
	}
}

// stageName is a small re-export so pipeline code reads in one vocabulary.
func stageName(s codev1.Stage) string { return queue.StageName(s) }
