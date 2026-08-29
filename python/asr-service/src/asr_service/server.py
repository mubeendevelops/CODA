"""asr-service entrypoint.

Consumes stage.asr and returns a valid StageResult per envelope. Phase 3's
walking-skeleton echo worker (see worker.py) — real Groq/pyannote
transcription replaces `handle_asr_stage`'s body in Phase 1, nothing else
here changes. This worker must never know about clinical fields or the
thought graph (docs/architecture.md §1.2).
"""

from __future__ import annotations

import asyncio
import signal

import redis.asyncio as aioredis

from asr_service.worker import handle_asr_stage
from coda_worker_sdk import (
    RedisConfig,
    ServiceConfig,
    StageWorker,
    StorageClient,
    StorageConfig,
    configure_logging,
)
from coda_worker_sdk.grpc_server import GrpcServer
from coda_worker_sdk.metrics import serve_http
from coda_worker_sdk.streams import GROUP_ASR_WORKERS, STREAM_ASR

SERVICE_NAME = "asr-service"


async def _amain() -> None:
    cfg = ServiceConfig.from_env(
        SERVICE_NAME, grpc_port_var="GRPC_PORT", health_port_var="HEALTH_PORT"
    )
    logger = configure_logging(SERVICE_NAME, cfg.log_level)

    redis_cfg = RedisConfig.from_env()
    redis = aioredis.Redis(
        host=redis_cfg.host,
        port=redis_cfg.port,
        password=redis_cfg.password or None,
        db=redis_cfg.db,
        decode_responses=True,
    )
    storage = StorageClient(StorageConfig.from_env())

    worker = StageWorker(
        service_name=SERVICE_NAME,
        redis=redis,
        storage=storage,
        stream=STREAM_ASR,
        group=GROUP_ASR_WORKERS,
        handler=handle_asr_stage,
        concurrency=4,
        batch=8,
    )

    grpc_server = GrpcServer(service_name=SERVICE_NAME, port=cfg.grpc_port)
    http_server, _http_thread = serve_http(cfg.health_port, SERVICE_NAME)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    await grpc_server.start()
    worker_task = asyncio.ensure_future(worker.run())
    logger.info(
        "listening",
        extra={
            "extra_fields": {
                "grpc_port": cfg.grpc_port,
                "health_port": cfg.health_port,
                "stream": STREAM_ASR,
            }
        },
    )

    await stop_event.wait()
    logger.info("shutting down")
    worker.stop()
    await worker_task
    await grpc_server.stop()
    http_server.shutdown()
    await redis.aclose()


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
