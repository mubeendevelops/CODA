package config

import (
	"fmt"
	"strconv"
)

// Storage is go-api's MinIO configuration (docs/architecture.md §3.2-3.3).
// Credentials come from the environment, same posture as DB — a service
// that touches object storage should fail at startup, not on first
// request, if misconfigured.
type Storage struct {
	Endpoint  string
	AccessKey string
	SecretKey string
	Bucket    string
	UseSSL    bool
}

// LoadStorage reads Storage from the environment (.env.example: MINIO_*).
func LoadStorage() Storage {
	useSSL, _ := strconv.ParseBool(getEnv("MINIO_USE_SSL", "false"))
	return Storage{
		Endpoint:  MustGetEnv("MINIO_ENDPOINT"),
		AccessKey: MustGetEnv("MINIO_ROOT_USER"),
		SecretKey: MustGetEnv("MINIO_ROOT_PASSWORD"),
		Bucket:    MustGetEnv("MINIO_BUCKET"),
		UseSSL:    useSSL,
	}
}

// Validate fails fast on a configuration that would silently misroute
// artifact keys — mirrors Server.Validate's posture.
func (s Storage) Validate() error {
	if s.Endpoint == "" {
		return fmt.Errorf("config: MINIO_ENDPOINT must not be empty")
	}
	if s.Bucket == "" {
		return fmt.Errorf("config: MINIO_BUCKET must not be empty")
	}
	return nil
}
