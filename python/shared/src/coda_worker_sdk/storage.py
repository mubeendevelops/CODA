"""MinIO artifact helpers: streaming get/put and the §3.3 artifact-key
naming, mirroring go/internal/storage/artifact.go and key.go field-for-field
so a key either side writes is parseable by the other.

Blocking MinIO SDK calls run in a thread via `asyncio.to_thread` so a worker
built on the asyncio consumer loop (consumer.py) never stalls the event loop
on I/O — the whole reason concurrency limits and prefetch in that loop mean
anything.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
from dataclasses import dataclass
from typing import BinaryIO

from minio import Minio
from minio.error import S3Error

from coda_worker_sdk.config import StorageConfig

# Mirrors the closed ArtifactKind enum in proto/coda/v1/common.proto and the
# Go-side closed set in go/internal/storage/artifact.go — a worker must not
# invent a kind, or the orchestrator's ParseArtifactKey rejects the result.
ARTIFACT_KINDS = frozenset(
    {
        "audio",
        "consent",
        "transcript",
        "thought_graph",
        "candidate_set",
        "clinical_note",
        "summary",
        "export_json",
        "export_pdf",
        "metrics",
        "redaction_map",
    }
)


@dataclass(frozen=True, slots=True)
class ArtifactKey:
    """A parsed stage-output key (architecture.md §3.3):

    {env}/consultations/{consultation_id}/stages/{stage}/{run_config_id}/{kind}.{ext}
    """

    env: str
    consultation_id: str
    stage: str
    run_config_id: str
    kind: str
    ext: str

    def __str__(self) -> str:
        return (
            f"{self.env}/consultations/{self.consultation_id}/stages/"
            f"{self.stage}/{self.run_config_id}/{self.kind}.{self.ext}"
        )


def build_artifact_key(
    env: str, consultation_id: str, stage: str, run_config_id: str, kind: str, ext: str
) -> str:
    """Builds a §3.3 stage-output key. Mirrors StageArtifactKey in
    go/internal/storage/artifact.go — same layout, same field order.
    """
    if kind not in ARTIFACT_KINDS:
        raise ValueError(f"coda_worker_sdk: {kind!r} is not a closed ArtifactKind value")
    return str(
        ArtifactKey(
            env=env,
            consultation_id=consultation_id,
            stage=stage,
            run_config_id=run_config_id,
            kind=kind,
            ext=ext,
        )
    )


def parse_artifact_key(key: str) -> ArtifactKey:
    """The inverse of build_artifact_key. Strict, matching
    go/internal/storage/artifact.go's ParseArtifactKey: a key that doesn't
    match the §3.3 layout, or names a kind outside the closed set, is an
    error rather than a best-effort parse.
    """
    parts = key.split("/")
    if len(parts) != 7 or parts[1] != "consultations" or parts[3] != "stages":
        raise ValueError(
            f"coda_worker_sdk: {key!r} is not a stage artifact key "
            "({env}/consultations/{id}/stages/{stage}/{run_config_id}/{kind}.{ext})"
        )
    env, _, consultation_id, _, stage, run_config_id, basename = parts
    if "." not in basename:
        raise ValueError(f"coda_worker_sdk: artifact key {key!r} has no extension")
    kind, _, ext = basename.rpartition(".")
    if kind not in ARTIFACT_KINDS:
        raise ValueError(
            f"coda_worker_sdk: artifact key {key!r} names kind {kind!r}, "
            "which is not one of the closed set in common.proto/ArtifactKind"
        )
    return ArtifactKey(
        env=env,
        consultation_id=consultation_id,
        stage=stage,
        run_config_id=run_config_id,
        kind=kind,
        ext=ext,
    )


class StorageClient:
    """Thin async wrapper over the MinIO SDK, scoped to one bucket."""

    def __init__(self, cfg: StorageConfig) -> None:
        self._cfg = cfg
        self._mc = Minio(
            cfg.endpoint,
            access_key=cfg.access_key,
            secret_key=cfg.secret_key,
            secure=cfg.use_ssl,
        )

    @property
    def env(self) -> str:
        return self._cfg.env

    @property
    def bucket(self) -> str:
        return self._cfg.bucket

    def artifact_key(
        self, consultation_id: str, stage: str, run_config_id: str, kind: str, ext: str
    ) -> str:
        return build_artifact_key(self._cfg.env, consultation_id, stage, run_config_id, kind, ext)

    async def get_bytes(self, key: str) -> bytes:
        """Reads an object fully into memory. Fine for the JSON-sized
        artifacts (transcripts, notes, graphs) this system ever writes;
        large objects (audio) should stream via get_stream instead.
        """

        def _get() -> bytes:
            resp = self._mc.get_object(self._cfg.bucket, key)
            try:
                return resp.read()
            finally:
                resp.close()
                resp.release_conn()

        return await asyncio.to_thread(_get)

    async def get_stream(
        self, key: str, dest: BinaryIO, *, chunk_size: int = 8 * 1024 * 1024
    ) -> None:
        """Streams an object to a file-like object without holding the whole
        thing in memory — the path for large source audio.
        """

        def _get() -> None:
            resp = self._mc.get_object(self._cfg.bucket, key)
            try:
                for chunk in resp.stream(chunk_size):
                    dest.write(chunk)
            finally:
                resp.close()
                resp.release_conn()

        await asyncio.to_thread(_get)

    async def put_bytes(
        self, key: str, data: bytes, *, content_type: str = "application/json"
    ) -> str:
        """Writes an object and returns its sha256 hex digest, stamped as
        `x-amz-meta-sha256` user metadata so StatArtifact
        (go/internal/storage/artifact.go) can read a real content hash
        rather than falling back to an ETag that isn't one for multipart
        uploads.
        """
        sha256 = hashlib.sha256(data).hexdigest()
        metadata: dict[str, str | list[str] | tuple[str]] = {"sha256": sha256}

        def _put() -> None:
            self._mc.put_object(
                self._cfg.bucket,
                key,
                io.BytesIO(data),
                length=len(data),
                content_type=content_type,
                metadata=metadata,
            )

        await asyncio.to_thread(_put)
        return sha256

    async def put_stream(
        self,
        key: str,
        src: BinaryIO,
        length: int,
        *,
        content_type: str = "application/octet-stream",
        sha256: str | None = None,
    ) -> None:
        """Streams a file-like object of known length to MinIO. The caller
        supplies `sha256` when it already knows the digest (e.g. the client
        declared it on upload); otherwise the object is written without a
        content-hash guarantee — callers writing large audio should compute
        the hash while streaming and pass it in rather than buffering twice.
        """
        metadata: dict[str, str | list[str] | tuple[str]] | None = (
            {"sha256": sha256} if sha256 else None
        )

        def _put() -> None:
            self._mc.put_object(
                self._cfg.bucket,
                key,
                src,
                length=length,
                content_type=content_type,
                metadata=metadata,
            )

        await asyncio.to_thread(_put)

    async def exists(self, key: str) -> bool:
        def _stat() -> bool:
            try:
                self._mc.stat_object(self._cfg.bucket, key)
                return True
            except S3Error as exc:
                if exc.code == "NoSuchKey":
                    return False
                raise

        return await asyncio.to_thread(_stat)


__all__ = [
    "ArtifactKey",
    "ARTIFACT_KINDS",
    "build_artifact_key",
    "parse_artifact_key",
    "StorageClient",
]
