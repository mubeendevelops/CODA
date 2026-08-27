// Package telemetry provides the structured JSON logger shared by every Go
// service. Every log line carries no PII — only redacted text is ever logged
// (docs/architecture.md §8).
package telemetry

import (
	"log/slog"
	"os"
	"strings"
)

// NewLogger returns a JSON slog.Logger at the given level ("debug", "info",
// "warn", "error"; unrecognized values fall back to "info"), tagged with the
// service name on every line.
func NewLogger(serviceName, level string) *slog.Logger {
	handler := slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{
		Level: parseLevel(level),
	})
	return slog.New(handler).With("service", serviceName)
}

func parseLevel(level string) slog.Level {
	switch strings.ToLower(level) {
	case "debug":
		return slog.LevelDebug
	case "warn", "warning":
		return slog.LevelWarn
	case "error":
		return slog.LevelError
	default:
		return slog.LevelInfo
	}
}
