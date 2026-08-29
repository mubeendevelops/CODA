"""Reusable worker SDK for CODA's Python services (docs/architecture.md §1.2, §2).

Everything a stage worker needs that is not clinical logic: the Redis Streams
transport, protojson codec, MinIO artifact helpers, heartbeats, structured
logging, metrics, an exception boundary, and a gRPC harness for the §2.6
synchronous calls. asr-service and nlp-service depend on this; it must never
depend on either of them.
"""

from __future__ import annotations

from coda_worker_sdk.config import RedisConfig, ServiceConfig, StorageConfig
from coda_worker_sdk.consumer import ConsumedMessage, StreamConsumer
from coda_worker_sdk.errors import (
    CancelledSignal,
    FatalError,
    QuotaExhaustedError,
    RetryableError,
    classify_exception,
)
from coda_worker_sdk.heartbeat import Heartbeater
from coda_worker_sdk.logging import configure_logging, current_trace_id, trace_context
from coda_worker_sdk.storage import ArtifactKey, StorageClient
from coda_worker_sdk.worker import StageContext, StageHandler, StageOutput, StageWorker

__all__ = [
    "RedisConfig",
    "ServiceConfig",
    "StorageConfig",
    "ConsumedMessage",
    "StreamConsumer",
    "CancelledSignal",
    "FatalError",
    "QuotaExhaustedError",
    "RetryableError",
    "classify_exception",
    "Heartbeater",
    "configure_logging",
    "current_trace_id",
    "trace_context",
    "ArtifactKey",
    "StorageClient",
    "StageContext",
    "StageHandler",
    "StageOutput",
    "StageWorker",
]
