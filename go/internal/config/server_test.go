package config

import (
	"testing"
	"time"
)

func validServer() Server {
	return Server{
		HTTPPort:              "8080",
		MetricsPort:           "9090",
		CORSAllowedOrigins:    []string{"http://localhost:5173"},
		RequestTimeout:        30 * time.Second,
		RateLimitRequests:     300,
		RateLimitWindow:       time.Minute,
		AuthRateLimitRequests: 20,
		AuthRateLimitWindow:   time.Minute,
	}
}

func TestServer_Validate(t *testing.T) {
	tests := []struct {
		name    string
		mutate  func(s Server) Server
		wantErr bool
	}{
		{"valid config", func(s Server) Server { return s }, false},
		{"empty http port", func(s Server) Server { s.HTTPPort = ""; return s }, true},
		{"empty metrics port", func(s Server) Server { s.MetricsPort = ""; return s }, true},
		{"no CORS origins", func(s Server) Server { s.CORSAllowedOrigins = nil; return s }, true},
		{"zero request timeout", func(s Server) Server { s.RequestTimeout = 0; return s }, true},
		{"negative request timeout", func(s Server) Server { s.RequestTimeout = -time.Second; return s }, true},
		{"zero rate limit requests", func(s Server) Server { s.RateLimitRequests = 0; return s }, true},
		{"zero rate limit window", func(s Server) Server { s.RateLimitWindow = 0; return s }, true},
		{"zero auth rate limit requests", func(s Server) Server { s.AuthRateLimitRequests = 0; return s }, true},
		{"zero auth rate limit window", func(s Server) Server { s.AuthRateLimitWindow = 0; return s }, true},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			err := tt.mutate(validServer()).Validate()
			if (err != nil) != tt.wantErr {
				t.Errorf("Validate() error = %v, wantErr %v", err, tt.wantErr)
			}
		})
	}
}

func TestGetEnvList(t *testing.T) {
	tests := []struct {
		name     string
		envValue string
		envSet   bool
		fallback []string
		want     []string
	}{
		{"unset uses fallback", "", false, []string{"a"}, []string{"a"}},
		{"empty string uses fallback", "", true, []string{"a"}, []string{"a"}},
		{"single value", "http://x.com", true, nil, []string{"http://x.com"}},
		{"comma separated with spaces trimmed", "http://a.com, http://b.com , http://c.com", true, nil, []string{"http://a.com", "http://b.com", "http://c.com"}},
	}

	const key = "CODA_TEST_ENV_LIST"
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Setenv(key, "")
			if tt.envSet {
				t.Setenv(key, tt.envValue)
			} else {
				t.Setenv(key, "")
				// t.Setenv can't unset; empty string exercises the same
				// "treat blank as unset" branch getEnvList implements.
			}
			got := getEnvList(key, tt.fallback)
			if len(got) != len(tt.want) {
				t.Fatalf("getEnvList() = %v, want %v", got, tt.want)
			}
			for i := range got {
				if got[i] != tt.want[i] {
					t.Errorf("getEnvList()[%d] = %q, want %q", i, got[i], tt.want[i])
				}
			}
		})
	}
}

func TestGetIntEnv(t *testing.T) {
	const key = "CODA_TEST_ENV_INT"

	t.Run("valid integer", func(t *testing.T) {
		t.Setenv(key, "42")
		if got := getIntEnv(key, 7); got != 42 {
			t.Errorf("getIntEnv() = %d, want 42", got)
		}
	})

	t.Run("unset falls back", func(t *testing.T) {
		got := getIntEnv("CODA_TEST_ENV_INT_UNSET", 7)
		if got != 7 {
			t.Errorf("getIntEnv() = %d, want 7", got)
		}
	})

	t.Run("invalid integer panics", func(t *testing.T) {
		t.Setenv(key, "not-a-number")
		defer func() {
			if recover() == nil {
				t.Error("expected a panic for an invalid integer env var")
			}
		}()
		getIntEnv(key, 7)
	})
}
