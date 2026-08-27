// Package pipeline will own the go-orchestrator's per-consultation state
// machine (docs/architecture.md §4): stage dispatch, retry/backoff, quota
// parking, and the CREATED..EXPORTED transition table. Implemented starting
// Phase 3. Empty in Phase 0 by design.
package pipeline
