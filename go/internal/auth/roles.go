// Package auth owns JWT issuance/verification, Argon2id password hashing,
// refresh-token rotation, and RBAC for go-api (docs/architecture.md §7.6).
//
// RBAC is admin | doctor | reviewer | auditor (claude_context.md decision
// #30 — supersedes the doctor | admin | researcher set that shipped with
// the initial users table): admin does everything doctor/reviewer can plus
// user management and audit-log read; doctor uploads, views, reviews,
// edits, approves, and exports own-org consultations; reviewer does the
// same review/edit/approve/view/export set as doctor minus upload, for a
// second clinician in a review workflow; auditor is read-only over
// audit_log and de-identified aggregates and has no access to source
// audio, unredacted transcripts, or clinical note content — the old
// researcher role's restriction under a new name.
package auth

// Role is a users.role value. Kept as a distinct type (not a bare string)
// so a handler declaring `[]Role{RoleAdmin}` can't typo a role name past
// the compiler the way a raw string literal could.
type Role string

const (
	RoleAdmin    Role = "admin"
	RoleDoctor   Role = "doctor"
	RoleReviewer Role = "reviewer"
	RoleAuditor  Role = "auditor"
)

// AllRoles mirrors the users.role CHECK constraint
// (go/migrations/000027_users_role_rbac.up.sql) — every value the database
// will accept, in one place so the two can be kept in sync deliberately.
var AllRoles = []Role{RoleAdmin, RoleDoctor, RoleReviewer, RoleAuditor}

// IsValid reports whether r is one of AllRoles.
func (r Role) IsValid() bool {
	for _, v := range AllRoles {
		if r == v {
			return true
		}
	}
	return false
}

// CanReadClinicalData reports whether the role may read consultation
// content, transcripts, or clinical notes at all. Only auditor is excluded
// — its access is limited to audit_log and de-identified aggregates
// (docs/architecture.md §7.6).
func (r Role) CanReadClinicalData() bool {
	return r == RoleAdmin || r == RoleDoctor || r == RoleReviewer
}

// CanManageUsers reports whether the role may register/deactivate users.
func (r Role) CanManageUsers() bool {
	return r == RoleAdmin
}

// CanReadAuditLog reports whether the role may read audit_log entries.
func (r Role) CanReadAuditLog() bool {
	return r == RoleAdmin || r == RoleAuditor
}
