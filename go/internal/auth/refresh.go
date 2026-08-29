package auth

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"fmt"
)

// refreshTokenBytes is the raw entropy of an opaque refresh token before
// encoding — 256 bits, well above what's brute-forceable, and irrelevant
// to guess even with the hash exposed since only the hash is ever stored
// (see HashRefreshToken).
const refreshTokenBytes = 32

// GenerateRefreshToken returns a URL-safe opaque token. Unlike the access
// token this is not a JWT — it carries no claims, so nothing about the
// user is recoverable from the token itself; that information lives only
// in the refresh_tokens row it hashes to.
func GenerateRefreshToken() (string, error) {
	b := make([]byte, refreshTokenBytes)
	if _, err := rand.Read(b); err != nil {
		return "", fmt.Errorf("auth: generate refresh token: %w", err)
	}
	return base64.RawURLEncoding.EncodeToString(b), nil
}

// HashRefreshToken returns the SHA-256 hex digest stored in
// refresh_tokens.token_hash. A refresh token is high-entropy random data,
// not a low-entropy secret an attacker could feasibly dictionary-attack
// from a stolen hash, so a fast hash is the correct tool here — unlike
// passwords, which use Argon2id (see password.go) specifically because
// they are guessable.
func HashRefreshToken(raw string) string {
	sum := sha256.Sum256([]byte(raw))
	return hex.EncodeToString(sum[:])
}
