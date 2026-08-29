package auth

import "testing"

func TestRole_IsValid(t *testing.T) {
	tests := []struct {
		role Role
		want bool
	}{
		{RoleAdmin, true},
		{RoleDoctor, true},
		{RoleReviewer, true},
		{RoleAuditor, true},
		{Role("researcher"), false}, // superseded role — must not silently work
		{Role(""), false},
		{Role("Admin"), false}, // case-sensitive
	}
	for _, tt := range tests {
		t.Run(string(tt.role), func(t *testing.T) {
			if got := tt.role.IsValid(); got != tt.want {
				t.Errorf("Role(%q).IsValid() = %v, want %v", tt.role, got, tt.want)
			}
		})
	}
}

func TestRole_CanReadClinicalData(t *testing.T) {
	tests := []struct {
		role Role
		want bool
	}{
		{RoleAdmin, true},
		{RoleDoctor, true},
		{RoleReviewer, true},
		{RoleAuditor, false},
	}
	for _, tt := range tests {
		if got := tt.role.CanReadClinicalData(); got != tt.want {
			t.Errorf("Role(%q).CanReadClinicalData() = %v, want %v", tt.role, got, tt.want)
		}
	}
}

func TestRole_CanReadAuditLog(t *testing.T) {
	tests := []struct {
		role Role
		want bool
	}{
		{RoleAdmin, true},
		{RoleAuditor, true},
		{RoleDoctor, false},
		{RoleReviewer, false},
	}
	for _, tt := range tests {
		if got := tt.role.CanReadAuditLog(); got != tt.want {
			t.Errorf("Role(%q).CanReadAuditLog() = %v, want %v", tt.role, got, tt.want)
		}
	}
}

func TestRole_CanManageUsers(t *testing.T) {
	tests := []struct {
		role Role
		want bool
	}{
		{RoleAdmin, true},
		{RoleDoctor, false},
		{RoleReviewer, false},
		{RoleAuditor, false},
	}
	for _, tt := range tests {
		if got := tt.role.CanManageUsers(); got != tt.want {
			t.Errorf("Role(%q).CanManageUsers() = %v, want %v", tt.role, got, tt.want)
		}
	}
}
