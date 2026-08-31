package config

import (
	"fmt"
	"strconv"
)

// Storage is go-api's MinIO configuration (docs/architecture.md §3.2-3.3).
// Credentials come from the environment, same posture as DB — a service
// that touches object storage should fail at startup, not on first
// request, if misconfigured.
//
// Endpoint and PublicEndpoint are deliberately separate. go-api itself
// (StatObject, bucket checks) must reach MinIO via the Docker-network
// hostname (Endpoint, e.g. "minio:9000"). A presigned URL, though, is
// consumed by a real browser on the host, which cannot resolve that
// hostname at all — SigV4 signs the Host header, so no client-side
// rewrite after signing can fix a URL signed for the wrong host (the
// exact problem decision #54 already named for scripts/e2e_smoke.py,
// which works around it by running *inside* the compose network instead;
// a browser has no such option). PublicEndpoint is the host-reachable
// address (e.g. "localhost:9010", matching this dev machine's port remap)
// that PresignPutObject/PresignGetObject sign against instead — computing
// a signature needs no live connection to that address, so a second
// minio.Client configured with it (same credentials) never needs to reach
// it except when the browser later does. Defaults to Endpoint when unset,
// which is correct whenever MinIO is already reachable at one address
// from everywhere (a real non-Docker-Compose deployment).
type Storage struct {
	Endpoint       string
	PublicEndpoint string
	AccessKey      string
	SecretKey      string
	Bucket         string
	UseSSL         bool
}

// LoadStorage reads Storage from the environment (.env.example: MINIO_*).
func LoadStorage() Storage {
	useSSL, _ := strconv.ParseBool(getEnv("MINIO_USE_SSL", "false"))
	endpoint := MustGetEnv("MINIO_ENDPOINT")
	return Storage{
		Endpoint:       endpoint,
		PublicEndpoint: getEnv("MINIO_PUBLIC_ENDPOINT", endpoint),
		AccessKey:      MustGetEnv("MINIO_ROOT_USER"),
		SecretKey:      MustGetEnv("MINIO_ROOT_PASSWORD"),
		Bucket:         MustGetEnv("MINIO_BUCKET"),
		UseSSL:         useSSL,
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
