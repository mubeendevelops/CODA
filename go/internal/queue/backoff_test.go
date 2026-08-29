package queue

import (
	"math/rand/v2"
	"testing"
	"time"
)

// fixedRand returns the same value from Float64, so a jittered delay
// becomes deterministic and the *ceiling* can be asserted exactly.
type fixedSource struct{ v uint64 }

func (f fixedSource) Uint64() uint64 { return f.v }

func randAt(fraction float64) *rand.Rand {
	// rand/v2 derives Float64 from the top 53 bits of Uint64, so feeding a
	// constant makes Float64 constant.
	return rand.New(fixedSource{v: uint64(fraction*(1<<53)) << 11})
}

func TestBackoffDelayFollowsFullJitterCeiling(t *testing.T) {
	// At the top of the jitter range the delay equals the ceiling, which is
	// what §2.5's `min(max_delay, base * 2^(attempt-1))` specifies.
	b := Backoff{Base: 2 * time.Second, Max: 120 * time.Second, Rand: randAt(0.999999999)}

	tests := []struct {
		attempt uint32
		ceiling time.Duration
	}{
		{1, 2 * time.Second},   // base * 2^0
		{2, 4 * time.Second},   // base * 2^1
		{3, 8 * time.Second},   // base * 2^2
		{6, 64 * time.Second},  // base * 2^5
		{7, 120 * time.Second}, // 128s would exceed max_delay, so clamped
		{9, 120 * time.Second},
	}
	for _, tc := range tests {
		got := b.Delay(tc.attempt)
		if got > tc.ceiling {
			t.Errorf("attempt %d: delay %s exceeds ceiling %s", tc.attempt, got, tc.ceiling)
		}
		// Within a millisecond of the ceiling, allowing for the float
		// rounding in the jitter multiply.
		if tc.ceiling-got > time.Millisecond {
			t.Errorf("attempt %d: delay %s is not at the %s ceiling", tc.attempt, got, tc.ceiling)
		}
	}
}

func TestBackoffIsFullJitterNotFixed(t *testing.T) {
	// The property that matters operationally: two jobs failing at the same
	// instant must not retry at the same instant. Sampling from [0, ceiling)
	// is what spreads them; a fixed exponential delay would not.
	b := Backoff{Base: 2 * time.Second, Max: 120 * time.Second}
	seen := make(map[time.Duration]int)
	for i := 0; i < 200; i++ {
		seen[b.Delay(5)]++
	}
	if len(seen) < 50 {
		t.Errorf("expected a wide spread of delays from full jitter, got %d distinct values in 200 draws", len(seen))
	}
	for d := range seen {
		if d < 0 || d > 32*time.Second {
			t.Errorf("delay %s is outside [0, base*2^4 = 32s]", d)
		}
	}
}

func TestBackoffDelayAtZeroJitterIsZero(t *testing.T) {
	b := Backoff{Base: 2 * time.Second, Max: 120 * time.Second, Rand: randAt(0)}
	if got := b.Delay(3); got != 0 {
		t.Errorf("full jitter must allow an immediate retry; got %s", got)
	}
}

func TestBackoffHandlesAbsurdAttemptWithoutOverflow(t *testing.T) {
	// A shift-overflow here would produce a zero or negative ceiling and
	// turn backoff into a hot retry loop — the one failure mode this
	// function exists to prevent.
	b := Backoff{Base: 2 * time.Second, Max: 120 * time.Second, Rand: randAt(0.999999999)}
	for _, attempt := range []uint32{0, 61, 62, 63, 1 << 20} {
		got := b.Delay(attempt)
		if got < 0 || got > 120*time.Second {
			t.Errorf("attempt %d produced an out-of-range delay %s", attempt, got)
		}
	}
}

func TestBackoffZeroValueUsesSpecDefaults(t *testing.T) {
	var b Backoff
	b.Rand = randAt(0.999999999)
	if got := b.Delay(1); got > 2*time.Second || 2*time.Second-got > time.Millisecond {
		t.Errorf("zero-value Backoff should fall back to the §2.5 base of 2s; attempt 1 gave %s", got)
	}
	if got := b.Delay(20); got > 120*time.Second {
		t.Errorf("zero-value Backoff should fall back to the §2.5 max of 120s; got %s", got)
	}
}

func TestResumeAtIsAbsolute(t *testing.T) {
	// Backoff is stored as jobs.resume_after rather than slept on, so a
	// crash during a 120s wait does not lose the retry.
	b := Backoff{Base: 10 * time.Second, Max: 10 * time.Second, Rand: randAt(0.999999999)}
	now := time.Date(2026, 8, 29, 12, 0, 0, 0, time.UTC)
	got := b.ResumeAt(now, 1)
	if !got.After(now) {
		t.Fatalf("ResumeAt must be in the future relative to its `now`; got %s for now=%s", got, now)
	}
	if d := got.Sub(now); d > 10*time.Second {
		t.Errorf("ResumeAt overshot the ceiling: %s", d)
	}
}
