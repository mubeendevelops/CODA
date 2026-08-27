// Package queue will own the Redis Streams producer/consumer-group client:
// XADD, XREADGROUP, XAUTOCLAIM stalled-message recovery, and stage.dlq
// routing per docs/architecture.md §2. Implemented in Phase 3. Empty in
// Phase 0 by design.
package queue
