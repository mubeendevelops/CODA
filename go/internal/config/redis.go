package config

import (
	"fmt"
	"strconv"
)

// Redis is the connection configuration for both roles Redis plays in this
// system (docs/architecture.md §1.2): the Streams transport between
// go-orchestrator and the Python workers, and the Asynq queue for
// Go-internal periodic jobs.
//
// One address, two logical databases. DB is the Streams database; AsynqDB
// is deliberately separate so `redis-cli -n <asynq> FLUSHDB` while
// debugging a stuck cron job cannot wipe pipeline streams, and so
// `KEYS *` on either side stays legible. ADR-0002 makes Redis persistence
// (AOF) a correctness concern rather than a cache setting — that is a
// deployment configuration, not something this struct can enforce.
type Redis struct {
	Addr     string
	Password string
	DB       int
	AsynqDB  int
}

// LoadRedis reads Redis from the environment (.env.example: REDIS_*).
// Password defaults empty: dev Compose runs an unauthenticated Redis on the
// internal network, the same documented plaintext-in-dev gap as §7.1.
func LoadRedis() Redis {
	db, _ := strconv.Atoi(getEnv("REDIS_DB", "0"))
	asynqDB, _ := strconv.Atoi(getEnv("REDIS_ASYNQ_DB", "1"))
	return Redis{
		Addr:     fmt.Sprintf("%s:%s", MustGetEnv("REDIS_HOST"), MustGetEnv("REDIS_PORT")),
		Password: getEnv("REDIS_PASSWORD", ""),
		DB:       db,
		AsynqDB:  asynqDB,
	}
}

// Validate mirrors Storage.Validate's posture: fail at startup on a
// configuration that would otherwise fail on the first XADD.
func (r Redis) Validate() error {
	if r.Addr == "" || r.Addr == ":" {
		return fmt.Errorf("config: REDIS_HOST/REDIS_PORT must not be empty")
	}
	if r.DB < 0 || r.AsynqDB < 0 {
		return fmt.Errorf("config: REDIS_DB/REDIS_ASYNQ_DB must not be negative, got %d/%d", r.DB, r.AsynqDB)
	}
	if r.DB == r.AsynqDB {
		return fmt.Errorf("config: REDIS_DB and REDIS_ASYNQ_DB must differ (both %d) — Asynq's keyspace and the pipeline streams must not share a database", r.DB)
	}
	return nil
}
