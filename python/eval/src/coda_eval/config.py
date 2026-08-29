"""Paths and Postgres connection config for the eval CLI. Runs on the host,
not in Docker (like `make seed` — see Makefile), so the Postgres defaults
are the host-side `localhost:$POSTGRES_HOST_PORT` mapping, not the
container-network `postgres:5432` the in-cluster services use.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
DATA_DIR = REPO_ROOT / "data"
DATA_RAW_DIR = DATA_DIR / "raw"
DATA_REGISTRY_DIR = DATA_DIR / "registry"
DATA_SPLITS_DIR = DATA_DIR / "splits"
DATA_PROCESSED_DIR = DATA_DIR / "processed"
DATASET_REGISTRY_PATH = DATA_DIR / "registry.yaml"
DOCS_EVAL_DIR = REPO_ROOT / "docs" / "eval"


def _get(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True, slots=True)
class PostgresConfig:
    host: str
    port: int
    user: str
    password: str
    database: str

    @classmethod
    def from_env(cls) -> PostgresConfig:
        return cls(
            host=_get("POSTGRES_HOST", "localhost"),
            port=int(_get("POSTGRES_HOST_PORT", "5433")),
            user=_get("POSTGRES_USER", "coda"),
            password=_get("POSTGRES_PASSWORD", ""),
            database=_get("POSTGRES_DB", "coda"),
        )

    def dsn(self) -> str:
        return (
            f"host={self.host} port={self.port} user={self.user} "
            f"password={self.password} dbname={self.database}"
        )


__all__ = [
    "DATASET_REGISTRY_PATH",
    "DATA_DIR",
    "DATA_PROCESSED_DIR",
    "DATA_RAW_DIR",
    "DATA_REGISTRY_DIR",
    "DATA_SPLITS_DIR",
    "DOCS_EVAL_DIR",
    "REPO_ROOT",
    "PostgresConfig",
]
