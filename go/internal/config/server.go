package config

import (
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

// Server is go-api's HTTP-layer configuration: ports, CORS, request
// timeout, and rate limiting (docs/architecture.md §1.1, §7.6 "rate
// limiting on upload and auth endpoints"). None of it is a secret, so
// unlike Auth it needs no log redaction.
type Server struct {
	HTTPPort    string
	MetricsPort string

	CORSAllowedOrigins []string

	RequestTimeout time.Duration

	// RateLimitRequests/RateLimitWindow bound the general API rate limit
	// (per client IP). AuthRateLimitRequests/AuthRateLimitWindow are
	// stricter and apply only to /v1/auth/* — those endpoints are the
	// brute-force/credential-stuffing surface.
	RateLimitRequests     int
	RateLimitWindow       time.Duration
	AuthRateLimitRequests int
	AuthRateLimitWindow   time.Duration
}

// LoadServer reads Server from the environment. Every field has a
// production-reasonable default so a bare `docker compose up` against
// .env.example works, per the same "fail fast only on secrets, default
// everything else" posture as Base (docs/architecture.md §0).
func LoadServer() Server {
	return Server{
		HTTPPort:              getEnv("GO_API_HTTP_PORT", "8080"),
		MetricsPort:           getEnv("GO_API_METRICS_PORT", "9090"),
		CORSAllowedOrigins:    getEnvList("CORS_ALLOWED_ORIGINS", []string{"http://localhost:5173"}),
		RequestTimeout:        getDurationEnv("HTTP_REQUEST_TIMEOUT", 30*time.Second),
		RateLimitRequests:     getIntEnv("RATE_LIMIT_REQUESTS", 300),
		RateLimitWindow:       getDurationEnv("RATE_LIMIT_WINDOW", time.Minute),
		AuthRateLimitRequests: getIntEnv("AUTH_RATE_LIMIT_REQUESTS", 20),
		AuthRateLimitWindow:   getDurationEnv("AUTH_RATE_LIMIT_WINDOW", time.Minute),
	}
}

// Validate fails fast on a configuration that would either not start
// (blank ports) or silently disable a safety control (a non-positive rate
// limit or timeout) rather than surface a startup error.
func (s Server) Validate() error {
	if s.HTTPPort == "" {
		return fmt.Errorf("config: GO_API_HTTP_PORT must not be empty")
	}
	if s.MetricsPort == "" {
		return fmt.Errorf("config: GO_API_METRICS_PORT must not be empty")
	}
	if len(s.CORSAllowedOrigins) == 0 {
		return fmt.Errorf("config: CORS_ALLOWED_ORIGINS must list at least one origin")
	}
	if s.RequestTimeout <= 0 {
		return fmt.Errorf("config: HTTP_REQUEST_TIMEOUT must be positive, got %s", s.RequestTimeout)
	}
	if s.RateLimitRequests <= 0 || s.RateLimitWindow <= 0 {
		return fmt.Errorf("config: RATE_LIMIT_REQUESTS/RATE_LIMIT_WINDOW must be positive, got %d/%s", s.RateLimitRequests, s.RateLimitWindow)
	}
	if s.AuthRateLimitRequests <= 0 || s.AuthRateLimitWindow <= 0 {
		return fmt.Errorf("config: AUTH_RATE_LIMIT_REQUESTS/AUTH_RATE_LIMIT_WINDOW must be positive, got %d/%s", s.AuthRateLimitRequests, s.AuthRateLimitWindow)
	}
	return nil
}

func getEnvList(key string, fallback []string) []string {
	v, ok := os.LookupEnv(key)
	if !ok || strings.TrimSpace(v) == "" {
		return fallback
	}
	parts := strings.Split(v, ",")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		p = strings.TrimSpace(p)
		if p != "" {
			out = append(out, p)
		}
	}
	return out
}

func getIntEnv(key string, fallback int) int {
	v, ok := os.LookupEnv(key)
	if !ok || v == "" {
		return fallback
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		panic(fmt.Sprintf("config: %s=%q is not a valid integer: %v", key, v, err))
	}
	return n
}
