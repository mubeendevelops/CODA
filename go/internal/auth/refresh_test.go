package auth

import "testing"

func TestGenerateRefreshToken_Uniqueness(t *testing.T) {
	seen := make(map[string]bool)
	for i := 0; i < 1000; i++ {
		tok, err := GenerateRefreshToken()
		if err != nil {
			t.Fatalf("GenerateRefreshToken: %v", err)
		}
		if tok == "" {
			t.Fatal("expected non-empty token")
		}
		if seen[tok] {
			t.Fatalf("generated duplicate token: %s", tok)
		}
		seen[tok] = true
	}
}

func TestHashRefreshToken_DeterministicAndDistinct(t *testing.T) {
	tok1, err := GenerateRefreshToken()
	if err != nil {
		t.Fatalf("GenerateRefreshToken: %v", err)
	}
	tok2, err := GenerateRefreshToken()
	if err != nil {
		t.Fatalf("GenerateRefreshToken: %v", err)
	}

	h1a := HashRefreshToken(tok1)
	h1b := HashRefreshToken(tok1)
	h2 := HashRefreshToken(tok2)

	if h1a != h1b {
		t.Error("hashing the same token twice must be deterministic")
	}
	if h1a == h2 {
		t.Error("hashing two different tokens must not collide")
	}
	if h1a == tok1 {
		t.Error("the hash must not equal the raw token")
	}
}
