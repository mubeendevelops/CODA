// Package config loads process configuration from the environment. Nothing
// here is service-specific — cmd/api and cmd/orchestrator both embed Base and
// add their own fields as later phases need them (DB/Redis/MinIO wiring is
// Phase 3; see docs/architecture.md §5).
package config

import (
	"fmt"
	"os"
)

// Base is the configuration every service reads regardless of role.
type Base struct {
	ServiceName string
	Env         string // dev | staging | prod — used in artifact key prefixes (architecture.md §3.3)
	LogLevel    string
}

// LoadBase reads Base from the environment, applying the same defaults across
// every service so `docker compose up` is healthy with only .env.example.
func LoadBase(serviceName string) Base {
	return Base{
		ServiceName: serviceName,
		Env:         getEnv("ENV", "dev"),
		LogLevel:    getEnv("LOG_LEVEL", "info"),
	}
}

// MustGetEnv reads a required environment variable or fails fast. Later
// phases use this for secrets (GROQ_API_KEY, HF_TOKEN, JWT signing key) that
// must never carry a silent default.
func MustGetEnv(key string) string {
	v, ok := os.LookupEnv(key)
	if !ok || v == "" {
		panic(fmt.Sprintf("config: required environment variable %q is not set", key))
	}
	return v
}

func getEnv(key, fallback string) string {
	if v, ok := os.LookupEnv(key); ok && v != "" {
		return v
	}
	return fallback
}
