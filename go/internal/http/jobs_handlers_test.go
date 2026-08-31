package http

import (
	"testing"
)

// TestRunConfigContentHash_Deterministic guards against the exact bug found
// live in this codebase: protojson.Marshal's output is not guaranteed
// byte-stable across repeated calls on an identical logical message, which
// silently defeated ADR-0012's content-hash interning — every job got its
// own uninterned run_config row regardless of whether an identical one
// already existed. Repeating the hash many times catches the
// non-determinism, which was intermittent (map/field ordering can happen to
// agree by chance on any single pair of calls).
func TestRunConfigContentHash_Deterministic(t *testing.T) {
	cfg := baseRunConfig("baseline", false, 1, 0, false, false)

	first, _, err := runConfigContentHash(cfg)
	if err != nil {
		t.Fatalf("runConfigContentHash: %v", err)
	}

	for i := 0; i < 50; i++ {
		got, _, err := runConfigContentHash(cfg)
		if err != nil {
			t.Fatalf("runConfigContentHash (iteration %d): %v", i, err)
		}
		if got != first {
			t.Fatalf("content_hash not stable across calls: iteration %d got %q, want %q", i, got, first)
		}
	}
}

// TestRunConfigContentHash_DistinctForDifferentConfigs guards the other
// direction: two genuinely different RunConfigs (different arm) must not
// collide onto the same hash.
func TestRunConfigContentHash_DistinctForDifferentConfigs(t *testing.T) {
	baseline := baseRunConfig("baseline", false, 1, 0, false, false)
	gotK2 := baseRunConfig("got_k2", true, 3, 2, true, false)

	h1, _, err := runConfigContentHash(baseline)
	if err != nil {
		t.Fatalf("runConfigContentHash(baseline): %v", err)
	}
	h2, _, err := runConfigContentHash(gotK2)
	if err != nil {
		t.Fatalf("runConfigContentHash(got_k2): %v", err)
	}
	if h1 == h2 {
		t.Fatalf("distinct arms produced the same content_hash %q", h1)
	}
}

// TestBaseRunConfig_AsrBackendMatchesRealImplementation guards against the
// other bug found live alongside the hash issue: AsrBackend must name the
// ASR backend asr-service actually runs (decision #55: faster-whisper only,
// Groq is not wired up), because queue.PolicyOptionsFor derives the ASR
// stage's soft deadline/visibility timeout from exactly this field. Getting
// it wrong doesn't fail loudly — it silently gives every real job the
// wrong (much shorter) timeout budget.
func TestBaseRunConfig_AsrBackendMatchesRealImplementation(t *testing.T) {
	for arm := range knownArms {
		cfg := knownArms[arm]()
		if cfg.GetAsrBackend() != "faster_whisper_local" {
			t.Errorf("arm %q: AsrBackend = %q, want %q (asr-service has no other real backend — decision #55)",
				arm, cfg.GetAsrBackend(), "faster_whisper_local")
		}
	}
}
