package queue

import (
	"crypto/sha256"
	"encoding/hex"

	"github.com/google/uuid"

	codev1 "coda/go/internal/genproto/coda/v1"
)

// idempotencySeparator delimits the four components below. docs/architecture.md
// §2.3 writes the key as a bare concatenation; an explicit separator is
// used because concatenation alone is ambiguous — two different component
// tuples could hash identically if any component's length varied. Three of
// the four are fixed-width today (two UUIDs and a sha256 hex), so this
// costs nothing and removes the class of bug entirely. The Python workers
// must use the same byte.
const idempotencySeparator = "\x1f" // ASCII unit separator

// IdempotencyKey is docs/architecture.md §2.3 / ADR-0007:
//
//	sha256(consultation_id ‖ stage ‖ run_config_id ‖ input_artifact_sha256)
//
// The two non-obvious components are deliberate:
//
//   - run_config_id — the same consultation processed under a different
//     ablation arm is *different work* that must actually run, not a
//     duplicate to be skipped. Without it the eval sweep would compute one
//     arm and silently reuse its output for the other five, which would not
//     look like a bug: it would look like the arms agreeing.
//   - input_artifact_sha256 — a stage whose input changed must re-run even
//     though every other component is unchanged.
//
// Everything about the dedupe guarantee follows from this key being stable
// across redeliveries and distinct across real work, and it is enforced by
// job_stages.idempotency_key UNIQUE rather than by any caller getting this
// right (ADR-0007).
func IdempotencyKey(consultationID uuid.UUID, stage codev1.Stage, runConfigID uuid.UUID, inputArtifactSHA256 string) string {
	h := sha256.New()
	h.Write([]byte(consultationID.String()))
	h.Write([]byte(idempotencySeparator))
	h.Write([]byte(StageName(stage)))
	h.Write([]byte(idempotencySeparator))
	h.Write([]byte(runConfigID.String()))
	h.Write([]byte(idempotencySeparator))
	h.Write([]byte(inputArtifactSHA256))
	return hex.EncodeToString(h.Sum(nil))
}
