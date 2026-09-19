"""A host-side MinIO client for `coda-eval` commands that need to write real
stage artifacts (thought graphs, notes, refinement traces) — currently only
`run-got-eval`, since `run-baseline-eval`'s single-pass arm writes no
artifacts of its own.

Mirrors `coda_eval.config.PostgresConfig`'s reasoning exactly: `coda_eval`
runs on the host, not inside Compose, so it must connect to MinIO's
published host port (`MINIO_PUBLIC_ENDPOINT`) rather than the docker-network
hostname `coda_worker_sdk.config.StorageConfig.from_env()` reads
(`MINIO_ENDPOINT`, e.g. "minio:9000") — that hostname does not resolve from
the host at all. Signing is done directly against whichever endpoint this
client is constructed with (no presigned URL is generated and handed to a
second party across a hostname rewrite), so this is not the SigV4
host-header problem `docker-compose.yml`'s `e2e` service comment describes.
"""

from __future__ import annotations

import os

from coda_worker_sdk.config import StorageConfig
from coda_worker_sdk.storage import StorageClient


def host_storage_client() -> StorageClient:
    endpoint = os.environ.get("MINIO_PUBLIC_ENDPOINT") or os.environ.get("MINIO_ENDPOINT", "")
    if not endpoint:
        raise RuntimeError(
            "coda_eval: MINIO_PUBLIC_ENDPOINT (or MINIO_ENDPOINT) must be set to reach MinIO "
            "from the host"
        )
    cfg = StorageConfig(
        endpoint=endpoint,
        access_key=os.environ.get("MINIO_ROOT_USER", ""),
        secret_key=os.environ.get("MINIO_ROOT_PASSWORD", ""),
        bucket=os.environ.get("MINIO_BUCKET", "coda"),
        use_ssl=os.environ.get("MINIO_USE_SSL", "false").lower() in ("1", "true", "yes"),
        env=os.environ.get("ENV", "dev"),
    )
    return StorageClient(cfg)


__all__ = ["host_storage_client"]
