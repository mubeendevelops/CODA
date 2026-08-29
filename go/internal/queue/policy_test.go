package queue

import (
	"testing"
	"time"

	"github.com/google/uuid"

	codev1 "coda/go/internal/genproto/coda/v1"
)

// TestPolicyMatchesArchitectureTable transcribes docs/architecture.md §4.2
// and asserts the code agrees with it. If the spec table changes, this test
// is the thing that fails.
func TestPolicyMatchesArchitectureTable(t *testing.T) {
	tests := []struct {
		name        string
		stage       codev1.Stage
		opts        PolicyOptions
		deadline    time.Duration
		visibility  time.Duration
		maxAttempts int32
	}{
		{"asr hosted whisper", codev1.Stage_STAGE_ASR, PolicyOptions{}, 5 * time.Minute, 10 * time.Minute, 3},
		{"asr local faster-whisper", codev1.Stage_STAGE_ASR, PolicyOptions{LocalASR: true}, 20 * time.Minute, 40 * time.Minute, 3},
		{"redact", codev1.Stage_STAGE_REDACT, PolicyOptions{}, 60 * time.Second, 120 * time.Second, 2},
		{"nlp baseline arm", codev1.Stage_STAGE_NLP, PolicyOptions{}, 10 * time.Minute, 20 * time.Minute, 3},
		{"nlp GoT arm", codev1.Stage_STAGE_NLP, PolicyOptions{GoTArm: true}, 45 * time.Minute, 90 * time.Minute, 3},
		{"export", codev1.Stage_STAGE_EXPORT, PolicyOptions{}, 60 * time.Second, 120 * time.Second, 2},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := PolicyFor(tc.stage, tc.opts)
			if got.SoftDeadline != tc.deadline {
				t.Errorf("SoftDeadline = %s, §4.2 says %s", got.SoftDeadline, tc.deadline)
			}
			if got.VisibilityTimeout != tc.visibility {
				t.Errorf("VisibilityTimeout = %s, §4.2 says %s", got.VisibilityTimeout, tc.visibility)
			}
			if got.MaxAttempts != tc.maxAttempts {
				t.Errorf("MaxAttempts = %d, §2.5 says %d", got.MaxAttempts, tc.maxAttempts)
			}
		})
	}
}

func TestPolicyVisibilityAlwaysExceedsDeadline(t *testing.T) {
	// A visibility timeout at or below the soft deadline would let the
	// reaper reclaim a message from a worker that is still legitimately
	// working on it, re-dispatching an expensive stage that is already in
	// progress. §4.2 sizes every window at roughly 2x for exactly this
	// reason, so it is asserted as an invariant rather than left to the
	// table above.
	for _, stage := range []codev1.Stage{
		codev1.Stage_STAGE_ASR, codev1.Stage_STAGE_REDACT,
		codev1.Stage_STAGE_NLP, codev1.Stage_STAGE_EXPORT,
	} {
		for _, opts := range []PolicyOptions{{}, {LocalASR: true}, {GoTArm: true}, {LocalASR: true, GoTArm: true}} {
			p := PolicyFor(stage, opts)
			if p.VisibilityTimeout <= p.SoftDeadline {
				t.Errorf("%s %+v: visibility timeout %s must exceed the soft deadline %s", stage, opts, p.VisibilityTimeout, p.SoftDeadline)
			}
		}
	}
}

func TestPolicyOptionsComeFromRunConfig(t *testing.T) {
	// Timeouts must be derived from the persisted RunConfig, not from the
	// environment — ADR-0012's "every result reconstructible from the
	// database alone" covers operational parameters too.
	got := PolicyOptionsFor(&codev1.RunConfig{AsrBackend: "faster_whisper_local", GotEnabled: true})
	if !got.LocalASR || !got.GoTArm {
		t.Errorf("PolicyOptionsFor did not read the run config: %+v", got)
	}
	got = PolicyOptionsFor(&codev1.RunConfig{AsrBackend: "groq", GotEnabled: false})
	if got.LocalASR || got.GoTArm {
		t.Errorf("PolicyOptionsFor invented options the run config did not set: %+v", got)
	}
	if got := PolicyOptionsFor(nil); got.LocalASR || got.GoTArm {
		t.Errorf("a nil run config must yield the hosted/baseline defaults, got %+v", got)
	}
}

func TestUnspecifiedStageGetsTheTightestPolicy(t *testing.T) {
	// An unspecified stage reaching dispatch is a bug. It must fail fast
	// rather than occupy a 90-minute visibility window.
	p := PolicyFor(codev1.Stage_STAGE_UNSPECIFIED, PolicyOptions{})
	if p.MaxAttempts != 1 || p.SoftDeadline > time.Minute {
		t.Errorf("unspecified stage should get the tightest policy, got %+v", p)
	}
}

func TestRequestStreamRouting(t *testing.T) {
	// redact and nlp share stage.nlp because nlp-service owns PII redaction
	// (§1.2) and §2.1 defines only two request streams.
	for stage, want := range map[codev1.Stage]string{
		codev1.Stage_STAGE_ASR:    StreamASR,
		codev1.Stage_STAGE_REDACT: StreamNLP,
		codev1.Stage_STAGE_NLP:    StreamNLP,
	} {
		got, err := RequestStreamFor(stage)
		if err != nil {
			t.Fatalf("RequestStreamFor(%s): %v", stage, err)
		}
		if got != want {
			t.Errorf("RequestStreamFor(%s) = %q, want %q", stage, got, want)
		}
	}

	// export must not be routable to a worker: §7.5 makes doctor approval
	// the only edge into it, so an orchestrator able to dispatch export
	// could route around the human review gate.
	if _, err := RequestStreamFor(codev1.Stage_STAGE_EXPORT); err == nil {
		t.Error("export must not be dispatchable to a worker stream")
	}
	if _, err := RequestStreamFor(codev1.Stage_STAGE_UNSPECIFIED); err == nil {
		t.Error("an unspecified stage must not resolve to a stream")
	}
}

func TestStageNameRoundTrip(t *testing.T) {
	for _, stage := range []codev1.Stage{
		codev1.Stage_STAGE_ASR, codev1.Stage_STAGE_REDACT,
		codev1.Stage_STAGE_NLP, codev1.Stage_STAGE_EXPORT,
	} {
		name := StageName(stage)
		if name == "" {
			t.Fatalf("StageName(%s) is empty", stage)
		}
		back, err := StageFromName(name)
		if err != nil || back != stage {
			t.Errorf("round trip of %s via %q gave %s, %v", stage, name, back, err)
		}
	}
	// STAGE_UNSPECIFIED has no persisted form — the pipeline_stage domain's
	// CHECK excludes it by design.
	if StageName(codev1.Stage_STAGE_UNSPECIFIED) != "" {
		t.Error("STAGE_UNSPECIFIED must have no pipeline_stage value")
	}
	if _, err := StageFromName("nonsense"); err == nil {
		t.Error("StageFromName must reject a value outside the domain")
	}
}

func TestIdempotencyKeySeparatesRealWorkFromRedelivery(t *testing.T) {
	consultation := uuid.MustParse("3fa85f64-5717-4562-b3fc-2c963f66afa6")
	armA := uuid.MustParse("7c9e6679-7425-40de-944b-e07fc1f90ae7")
	armB := uuid.MustParse("b2f1a4d0-9c3e-4b7a-8f2d-1e6a9d5c7f31")
	const inputSHA = "9f2a1c4e5d6b7a8c9e0f1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f"

	base := IdempotencyKey(consultation, codev1.Stage_STAGE_NLP, armA, inputSHA)

	// A redelivery of the same work unit is the same key — that is what
	// makes the dedupe check absorb it.
	if again := IdempotencyKey(consultation, codev1.Stage_STAGE_NLP, armA, inputSHA); again != base {
		t.Error("the same work unit must hash to the same key across redeliveries")
	}

	// The three ways the work genuinely differs must all produce a
	// different key. The run_config_id case is the one that matters most:
	// if two ablation arms collided, the eval sweep would compute one arm
	// and silently reuse its output for the other — which would not look
	// like a bug, it would look like the arms agreeing.
	for name, got := range map[string]string{
		"different arm":   IdempotencyKey(consultation, codev1.Stage_STAGE_NLP, armB, inputSHA),
		"different stage": IdempotencyKey(consultation, codev1.Stage_STAGE_REDACT, armA, inputSHA),
		"different input": IdempotencyKey(consultation, codev1.Stage_STAGE_NLP, armA, "0000000000000000000000000000000000000000000000000000000000000000"),
		"different consultation": IdempotencyKey(
			uuid.MustParse("11111111-1111-4111-8111-111111111111"), codev1.Stage_STAGE_NLP, armA, inputSHA),
	} {
		if got == base {
			t.Errorf("%s must produce a distinct idempotency key", name)
		}
	}

	if len(base) != 64 {
		t.Errorf("idempotency key should be a hex sha256 digest, got %d chars", len(base))
	}
}
