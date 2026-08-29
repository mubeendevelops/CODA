package pipeline

import (
	"testing"

	codev1 "coda/go/internal/genproto/coda/v1"
)

// TestRedactionIsUnskippable is the structural half of ADR-0014 / §7.3.
// Redaction is the last point before transcript text reaches a third-party
// LLM, so "NLP must never run on unredacted text" has to be a property of
// the state machine, not a convention any worker bug could break.
func TestRedactionIsUnskippable(t *testing.T) {
	// There is no rest state from which nlp is dispatched that asr can
	// reach without passing through redact.
	if next, _ := nextAfter(codev1.Stage_STAGE_ASR); next != StateASRDone {
		t.Fatalf("asr must lead to asr_done (the state redact dispatches from), got %s", next)
	}
	if stage, ok := stageForState(StateASRDone); !ok || stage != codev1.Stage_STAGE_REDACT {
		t.Fatalf("asr_done must dispatch redact, got %s (ok=%v)", stage, ok)
	}
	if next, _ := nextAfter(codev1.Stage_STAGE_REDACT); next != StateRedacted {
		t.Fatalf("redact must lead to redacted, got %s", next)
	}
	if stage, ok := stageForState(StateRedacted); !ok || stage != codev1.Stage_STAGE_NLP {
		t.Fatalf("redacted must dispatch nlp, got %s (ok=%v)", stage, ok)
	}

	// And no state reachable from asr dispatches nlp directly.
	for state, stage := range restStateAliases {
		if stage != codev1.Stage_STAGE_NLP {
			continue
		}
		if state != StateRedacted && state != StateNLPQueued {
			t.Errorf("state %q dispatches nlp without having passed through redaction", state)
		}
	}
}

// TestExportIsNotAutomatic guards §7.5's "no auto-save before doctor
// approval" at the state-machine layer: the orchestrator must have no path
// that reaches APPROVED or dispatches export.
func TestExportIsNotAutomatic(t *testing.T) {
	for _, st := range autoPipeline {
		if st.Stage == codev1.Stage_STAGE_EXPORT {
			t.Fatal("export must not be part of the automatic pipeline — only a doctor's approval leads there (§7.5)")
		}
	}
	for state := range restStateAliases {
		if state == StateApproved || state == StateAwaitingReview || state == StateUnderReview {
			t.Errorf("state %q must not be dispatchable by the orchestrator — it is downstream of the human review gate", state)
		}
	}
	if !IsTerminal(StateApproved) || !IsTerminal(StateExported) {
		t.Error("approved/exported must be terminal for the orchestrator — they belong to go-api")
	}
}

func TestFinalStageStopsAtAwaitingReview(t *testing.T) {
	// §4.1: NLP_DONE writes the note as a draft and the pipeline waits for
	// a human. The orchestrator must stop there, not roll onward.
	next, ok := nextAfter(codev1.Stage_STAGE_NLP)
	if !ok || next != StateAwaitingReview {
		t.Fatalf("nlp success must lead to awaiting_review, got %s (ok=%v)", next, ok)
	}
	if _, dispatchable := stageForState(StateAwaitingReview); dispatchable {
		t.Error("awaiting_review must not dispatch anything")
	}
}

func TestResumeStateStopsAtFirstIncompleteStage(t *testing.T) {
	tests := []struct {
		name      string
		succeeded []codev1.Stage
		want      State
	}{
		{"nothing done", nil, StateASRQueued},
		{"asr done", []codev1.Stage{codev1.Stage_STAGE_ASR}, StateASRDone},
		{"asr+redact done", []codev1.Stage{codev1.Stage_STAGE_ASR, codev1.Stage_STAGE_REDACT}, StateNLPQueued},
		{"all done", []codev1.Stage{codev1.Stage_STAGE_ASR, codev1.Stage_STAGE_REDACT, codev1.Stage_STAGE_NLP}, StateAwaitingReview},
		// A later stage succeeding without an earlier one is not a real
		// pipeline state, but the resume computation must still refuse to
		// skip the missing stage rather than jumping ahead.
		{"gap in the middle", []codev1.Stage{codev1.Stage_STAGE_ASR, codev1.Stage_STAGE_NLP}, StateASRDone},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			done := make(map[codev1.Stage]bool)
			for _, s := range tc.succeeded {
				done[s] = true
			}
			if got := resumeState(done); got != tc.want {
				t.Errorf("resumeState = %s, want %s", got, tc.want)
			}
		})
	}
}

func TestParkStateReturnsToADispatchableState(t *testing.T) {
	// A parked job (quota exhaustion or retry backoff) must land somewhere
	// the dispatch scan looks, or it waits forever. redact is the
	// interesting case: §4.1 defines no REDACT_QUEUED, so it parks in
	// ASR_DONE — the state it is dispatched from.
	for _, stage := range []codev1.Stage{
		codev1.Stage_STAGE_ASR, codev1.Stage_STAGE_REDACT, codev1.Stage_STAGE_NLP,
	} {
		parked := parkState(stage)
		got, ok := stageForState(parked)
		if !ok {
			t.Errorf("%s parks in %q, which the dispatch scan does not look at", stage, parked)
			continue
		}
		if got != stage {
			t.Errorf("%s parks in %q, which dispatches %s instead", stage, parked, got)
		}
	}
}

func TestStageStatusMapping(t *testing.T) {
	for status, want := range map[codev1.Status]string{
		codev1.Status_STATUS_OK:              "succeeded",
		codev1.Status_STATUS_QUOTA_EXHAUSTED: "quota_exhausted",
		codev1.Status_STATUS_CANCELLED:       "cancelled",
		codev1.Status_STATUS_RETRYABLE:       "failed",
		codev1.Status_STATUS_FATAL:           "failed",
		codev1.Status_STATUS_UNSPECIFIED:     "failed",
	} {
		if got := stageStatusFor(status); got != want {
			t.Errorf("stageStatusFor(%s) = %q, want %q", status, got, want)
		}
	}
}
