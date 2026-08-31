"""nlp-service entrypoint.

Consumes stage.nlp — both STAGE_REDACT and STAGE_NLP envelopes
(architecture.md §1.2) — and returns a valid StageResult per envelope.
STAGE_REDACT is still an echo (real redaction is separate, unbuilt work).
STAGE_NLP now runs real single-pass extraction (Phase 4) for the
`got_enabled = false` arm; the GoT arm is Phase 6, not built yet. This
worker must never touch audio or perform diarization/ASR.
"""

from __future__ import annotations

import asyncio
import signal

import redis.asyncio as aioredis

from coda_worker_sdk import (
    PostgresConfig,
    PostgresPool,
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
from nlp_service.config import NlpConfig
from nlp_service.llm.groq import GroqLLMClient
from nlp_service.worker import build_nlp_handler

SERVICE_NAME = "nlp-service"


async def _amain() -> None:
    cfg = ServiceConfig.from_env(
        SERVICE_NAME, grpc_port_var="GRPC_PORT", health_port_var="HEALTH_PORT"
    )
    logger = configure_logging(SERVICE_NAME, cfg.log_level)
    nlp_cfg = NlpConfig.from_env()

    redis_cfg = RedisConfig.from_env()
    redis = aioredis.Redis(
        host=redis_cfg.host,
        port=redis_cfg.port,
        password=redis_cfg.password or None,
        db=redis_cfg.db,
        decode_responses=True,
    )
    storage = StorageClient(StorageConfig.from_env())

    pg_pool = PostgresPool(PostgresConfig.from_env())
    await pg_pool.open()

    llm_client = GroqLLMClient(api_key=nlp_cfg.groq_api_key)

    handler = build_nlp_handler(
        pg_pool=pg_pool,
        llm_client=llm_client,
        repair_max_attempts=nlp_cfg.json_repair_max_attempts,
        timeout_s=nlp_cfg.llm_timeout_s,
    )

    worker = StageWorker(
        service_name=SERVICE_NAME,
        redis=redis,
        storage=storage,
        stream=STREAM_NLP,
        group=GROUP_NLP_WORKERS,
        handler=handler,
        concurrency=4,
        batch=8,
        # Must exceed real worst-case wall-clock for whichever of
        # STAGE_REDACT/STAGE_NLP this worker is mid-handling, or the periodic
        # reclaim sweep self-reclaims a message still legitimately in flight
        # and dispatches a duplicate run the moment the original finishes
        # (found live in asr-service against the SDK's 5-minute default —
        # see asr_service/server.py's StageWorker for the full failure mode).
        # 90 minutes matches go/internal/queue/policy.go's
        # PolicyFor(STAGE_NLP, GoTArm=true).VisibilityTimeout — the ceiling
        # across every arm this worker will ever see, present (baseline,
        # 20 min) and future (GoT, Phase 6, 90 min) — the two must not drift
        # independently.
        min_idle_ms=90 * 60 * 1000,
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
    await pg_pool.close()


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
