package queue

import (
	"math/rand/v2"
	"time"
)

// Backoff implements docs/architecture.md §2.5's retry delay:
//
//	delay = random(0, min(max_delay, base * 2^(attempt-1)))
//
// This is *full* jitter, not "exponential plus a little noise". The
// distinction matters here: the retryable failures this system actually
// sees are provider-side (Groq 5xx, model overload), so every in-flight
// consultation in an eval sweep tends to fail at the same instant. Equal
// backoffs would re-synchronise them into the same thundering retry;
// sampling uniformly from [0, ceiling) spreads them across the whole
// window instead.
type Backoff struct {
	// Base is the first attempt's ceiling. §2.5: 2s.
	Base time.Duration
	// Max caps the ceiling however many attempts have failed. §2.5: 120s.
	Max time.Duration
	// Rand is the source of jitter. Nil means the global rand/v2 source;
	// tests inject a deterministic one to assert on exact delays.
	Rand *rand.Rand
}

// DefaultBackoff is §2.5's policy: base 2s, max 120s.
var DefaultBackoff = Backoff{Base: 2 * time.Second, Max: 120 * time.Second}

// Delay returns the wait before attempt number `attempt` (1-based, matching
// StageEnvelope.attempt). Attempt 1 is the first *retry* ceiling, per the
// 2^(attempt-1) exponent in §2.5.
func (b Backoff) Delay(attempt uint32) time.Duration {
	base := b.Base
	if base <= 0 {
		base = DefaultBackoff.Base
	}
	maxDelay := b.Max
	if maxDelay <= 0 {
		maxDelay = DefaultBackoff.Max
	}

	if attempt < 1 {
		attempt = 1
	}
	// Shift instead of math.Pow, and clamp the exponent before shifting: a
	// job that somehow reached attempt 64 would otherwise overflow the
	// shift into a negative or zero ceiling, turning backoff into a hot
	// loop — the exact failure this function exists to prevent.
	ceiling := maxDelay
	if exp := attempt - 1; exp < 62 {
		if scaled := base * time.Duration(int64(1)<<exp); scaled > 0 && scaled < maxDelay {
			ceiling = scaled
		}
	}

	return time.Duration(b.float64() * float64(ceiling))
}

// ResumeAt converts a Delay into the absolute timestamp stored in
// jobs.resume_after, which is what the dispatch scan compares against.
// Backoff is expressed as a parked job with a wake-up time rather than an
// in-process sleep so it survives an orchestrator restart — a crash during
// a 120s backoff must not lose the retry.
func (b Backoff) ResumeAt(now time.Time, attempt uint32) time.Time {
	return now.Add(b.Delay(attempt))
}

func (b Backoff) float64() float64 {
	if b.Rand != nil {
		return b.Rand.Float64()
	}
	return rand.Float64() //nolint:gosec // jitter, not a security decision
}
