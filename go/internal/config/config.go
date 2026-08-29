// Package config loads process configuration from the environment. Nothing
// here is service-specific — cmd/api and cmd/orchestrator both embed Base and
// add their own fields as later phases need them (Redis/MinIO wiring is
// still Phase 3; see docs/architecture.md §5).
package config

import (
	"fmt"
	"net/url"
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

// DB is the Postgres connection configuration (docs/architecture.md §5).
type DB struct {
	Host     string
	Port     string
	User     string
	Password string
	Name     string
}

// LoadDB reads DB from the environment. All fields are required — a
// service that touches Postgres should fail at startup, not on first query,
// if the environment is misconfigured.
func LoadDB() DB {
	return DB{
		Host:     MustGetEnv("POSTGRES_HOST"),
		Port:     MustGetEnv("POSTGRES_PORT"),
		User:     MustGetEnv("POSTGRES_USER"),
		Password: MustGetEnv("POSTGRES_PASSWORD"),
		Name:     MustGetEnv("POSTGRES_DB"),
	}
}

// DSN renders a postgres:// connection string. Credentials are URL-escaped
// since the dev default password (.env.example) contains no special
// characters today but a rotated one might.
func (d DB) DSN() string {
	u := url.URL{
		Scheme: "postgres",
		User:   url.UserPassword(d.User, d.Password),
		Host:   fmt.Sprintf("%s:%s", d.Host, d.Port),
		Path:   "/" + d.Name,
	}
	q := u.Query()
	q.Set("sslmode", "disable") // dev Compose runs plaintext (ADR-0014); TLS is a production config
	u.RawQuery = q.Encode()
	return u.String()
}

func getEnv(key, fallback string) string {
	if v, ok := os.LookupEnv(key); ok && v != "" {
		return v
	}
	return fallback
}
