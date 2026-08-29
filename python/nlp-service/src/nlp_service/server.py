"""nlp-service entrypoint.

Consumes stage.nlp — both STAGE_REDACT and STAGE_NLP envelopes
(architecture.md §1.2) — and returns a valid StageResult per envelope.
Phase 3's walking-skeleton echo worker (see worker.py); real redaction and
GoT-lite reasoning replace `handle_nlp_stage`'s dispatch targets in Phases
6/7, nothing here changes. This worker must never touch audio or perform
diarization/ASR.
"""

from __future__ import annotations

import asyncio
import signal

import redis.asyncio as aioredis

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
from coda_worker_sdk.streams import GROUP_NLP_WORKERS, STREAM_NLP
from nlp_service.worker import handle_nlp_stage

SERVICE_NAME = "nlp-service"


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
        stream=STREAM_NLP,
        group=GROUP_NLP_WORKERS,
        handler=handle_nlp_stage,
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
                "stream": STREAM_NLP,
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
