// Package audit will own append-only audit_log writes, issued in the same
// transaction as the action being audited (docs/architecture.md §7.4).
// Implemented in Phase 3. Empty in Phase 0 by design.
package audit
