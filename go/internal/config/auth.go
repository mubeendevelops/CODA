package config

import (
	"fmt"
	"log/slog"
	"os"
	"time"
)

// minSigningKeyLen is a floor, not a recommendation — HS256 wants at least
// as many bits of key as the hash produces (32 bytes) to not be the weak
// link, but the dev placeholder in .env.example is intentionally shorter
// than that so `docker compose up` still works out of the box. 16 bytes is
// the line between "a real secret, however short" and "empty/placeholder
// left unset by mistake", which is the failure this check exists to catch.
const minSigningKeyLen = 16

// Auth is the JWT/session configuration for go-api (docs/architecture.md
// §7.6). JWTSigningKey is a secret: it is deliberately unexported from
// String()/LogValue() so an accidental `slog.Info("cfg", "auth", cfg)`
// cannot leak it — every log line must stay secret-free per the task
// instructions and claude_context.md §8's "no secret ever logged" posture.
type Auth struct {
	JWTSigningKey   string
	AccessTokenTTL  time.Duration
	RefreshTokenTTL time.Duration
}

// LogValue implements slog.LogValuer so Auth never prints its signing key,
// even if a caller logs the struct directly instead of a specific field.
func (a Auth) LogValue() slog.Value {
	return slog.GroupValue(
		slog.Int("jwt_signing_key_len", len(a.JWTSigningKey)),
		slog.Duration("access_token_ttl", a.AccessTokenTTL),
		slog.Duration("refresh_token_ttl", a.RefreshTokenTTL),
	)
}

func (a Auth) String() string {
	return fmt.Sprintf("Auth{jwt_signing_key_len=%d access_token_ttl=%s refresh_token_ttl=%s}",
		len(a.JWTSigningKey), a.AccessTokenTTL, a.RefreshTokenTTL)
}

// LoadAuth reads Auth from the environment. JWT_SIGNING_KEY is a required
// secret (MustGetEnv: fail fast at startup, never at first request); the
// TTLs have sane defaults so most deployments never need to set them.
func LoadAuth() Auth {
	return Auth{
		JWTSigningKey:   MustGetEnv("JWT_SIGNING_KEY"),
		AccessTokenTTL:  getDurationEnv("JWT_ACCESS_TOKEN_TTL", 15*time.Minute),
		RefreshTokenTTL: getDurationEnv("JWT_REFRESH_TOKEN_TTL", 7*24*time.Hour),
	}
}

// Validate fails fast on a signing key too short or weak to trust, rather
// than accepting it and producing forgeable tokens at runtime.
func (a Auth) Validate() error {
	if len(a.JWTSigningKey) < minSigningKeyLen {
		return fmt.Errorf("config: JWT_SIGNING_KEY must be at least %d bytes, got %d", minSigningKeyLen, len(a.JWTSigningKey))
	}
	if a.AccessTokenTTL <= 0 {
		return fmt.Errorf("config: JWT_ACCESS_TOKEN_TTL must be positive, got %s", a.AccessTokenTTL)
	}
	if a.RefreshTokenTTL <= a.AccessTokenTTL {
		return fmt.Errorf("config: JWT_REFRESH_TOKEN_TTL (%s) must be longer than JWT_ACCESS_TOKEN_TTL (%s)", a.RefreshTokenTTL, a.AccessTokenTTL)
	}
	return nil
}

func getDurationEnv(key string, fallback time.Duration) time.Duration {
	v, ok := os.LookupEnv(key)
	if !ok || v == "" {
		return fallback
	}
	d, err := time.ParseDuration(v)
	if err != nil {
		panic(fmt.Sprintf("config: %s=%q is not a valid duration: %v", key, v, err))
	}
	return d
}
