package auth

import "testing"

func TestHashPassword_ProducesArgon2idFormat(t *testing.T) {
	hash, err := HashPassword("correct-horse-battery-staple")
	if err != nil {
		t.Fatalf("HashPassword: %v", err)
	}
	if hash == "" {
		t.Fatal("expected non-empty hash")
	}
	if hash[:9] != "$argon2id" {
		t.Fatalf("expected argon2id-prefixed hash, got %q", hash[:min(20, len(hash))])
	}
}

func TestHashPassword_SaltsDifferently(t *testing.T) {
	h1, err := HashPassword("same-password")
	if err != nil {
		t.Fatalf("HashPassword: %v", err)
	}
	h2, err := HashPassword("same-password")
	if err != nil {
		t.Fatalf("HashPassword: %v", err)
	}
	if h1 == h2 {
		t.Fatal("two hashes of the same password with independent salts must differ")
	}
}

func TestVerifyPassword(t *testing.T) {
	hash, err := HashPassword("s3cr3t-P@ssw0rd")
	if err != nil {
		t.Fatalf("HashPassword: %v", err)
	}

	tests := []struct {
		name      string
		hash      string
		candidate string
		want      bool
		wantErr   bool
	}{
		{"correct password", hash, "s3cr3t-P@ssw0rd", true, false},
		{"wrong password", hash, "wrong-password", false, false},
		{"empty candidate against real hash", hash, "", false, false},
		{"case-sensitive mismatch", hash, "S3CR3T-P@SSW0RD", false, false},
		{"malformed hash", "not-a-real-hash", "s3cr3t-P@ssw0rd", false, true},
		{"empty hash", "", "s3cr3t-P@ssw0rd", false, true},
		{"truncated hash", "$argon2id$v=19$m=65536,t=1,p=4$onlysalt", "s3cr3t-P@ssw0rd", false, true},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := VerifyPassword(tt.hash, tt.candidate)
			if (err != nil) != tt.wantErr {
				t.Fatalf("VerifyPassword() error = %v, wantErr %v", err, tt.wantErr)
			}
			if got != tt.want {
				t.Errorf("VerifyPassword() = %v, want %v", got, tt.want)
			}
		})
	}
}

func TestVerifyPassword_RejectsBcryptHash(t *testing.T) {
	// Regression guard: this project migrated from bcrypt to Argon2id
	// (claude_context.md decision). A bcrypt-formatted hash must fail
	// closed (error, not a false "match"), never be silently accepted.
	bcryptLike := "$2a$10$lUdf1/ppqdLG0abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMN"
	got, err := VerifyPassword(bcryptLike, "anything")
	if err == nil {
		t.Fatal("expected an error decoding a non-argon2id hash")
	}
	if got {
		t.Fatal("must never report a match for an unparseable hash")
	}
}
