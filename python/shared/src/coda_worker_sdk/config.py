"""Environment configuration, mirroring go/internal/config's shape and the
.env.example variable names — one Redis/MinIO deployment, read the same way
on both sides of the transport.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _require(name: str) -> str:
    val = os.environ.get(name, "")
    if not val:
        raise RuntimeError(f"coda_worker_sdk: required environment variable {name} is not set")
    return val


def _get(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True, slots=True)
class RedisConfig:
    """Connection config for the Redis Streams transport (ADR-0002).

    DB is deliberately the *streams* database, distinct from Asynq's — see
    go/internal/config/redis.go. Python workers never touch the Asynq DB.
    """

    host: str
    port: int
    password: str
    db: int

    @classmethod
    def from_env(cls) -> RedisConfig:
        return cls(
            host=_require("REDIS_HOST"),
            port=int(_get("REDIS_PORT", "6379")),
            password=_get("REDIS_PASSWORD", ""),
            db=int(_get("REDIS_DB", "0")),
        )


@dataclass(frozen=True, slots=True)
class StorageConfig:
    """MinIO connection config, matching go/internal/config's Storage struct."""

    endpoint: str
    access_key: str
    secret_key: str
    bucket: str
    use_ssl: bool
    env: str
    """Artifact-key environment prefix (architecture.md §3.3), e.g. "dev"."""

    @classmethod
    def from_env(cls) -> StorageConfig:
        return cls(
            endpoint=_require("MINIO_ENDPOINT"),
            access_key=_require("MINIO_ROOT_USER"),
            secret_key=_require("MINIO_ROOT_PASSWORD"),
            bucket=_get("MINIO_BUCKET", "coda"),
            use_ssl=_get("MINIO_USE_SSL", "false").lower() in ("1", "true", "yes"),
            env=_get("ENV", "dev"),
        )


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    """Process-level configuration shared by every worker entrypoint."""

    service_name: str
    log_level: str
    grpc_port: int
    health_port: int

    @classmethod
    def from_env(
        cls, service_name: str, *, grpc_port_var: str, health_port_var: str
    ) -> ServiceConfig:
        return cls(
            service_name=service_name,
            log_level=_get("LOG_LEVEL", "INFO").upper(),
            grpc_port=int(_require(grpc_port_var)),
            health_port=int(_require(health_port_var)),
        )
