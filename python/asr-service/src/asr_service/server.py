"""asr-service entrypoint.

Consumes stage.asr and returns a valid StageResult per envelope. Loads
faster-whisper + pyannote once here at process startup (asr_service.models)
before the consumer loop ever starts, so no message pays model-load cost and
none can be dispatched before the models are ready. This worker must never
know about clinical fields or the thought graph (docs/architecture.md §1.2).
"""

from __future__ import annotations

import asyncio
import signal

import redis.asyncio as aioredis

from asr_service.config import AsrConfig
from asr_service.models import ModelBundle, load_models
from asr_service.worker import build_asr_handler
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

    asr_cfg = AsrConfig.from_env()  # fails fast if HF_TOKEN / GROQ_API_KEY are unset

    redis_cfg = RedisConfig.from_env()
    redis = aioredis.Redis(
        host=redis_cfg.host,
        port=redis_cfg.port,
        password=redis_cfg.password or None,
        db=redis_cfg.db,
        decode_responses=True,
    )
    storage = StorageClient(StorageConfig.from_env())

    # /readyz reports not-ready until model loading finishes below — see
    # docker-compose.yml's asr-service healthcheck, which polls /readyz so
    # `depends_on: condition: service_healthy` actually waits on this.
    holder: dict[str, ModelBundle | None] = {"bundle": None}

    def _ready() -> bool:
        bundle = holder["bundle"]
        return bundle is not None and bundle.ready

    http_server, _http_thread = serve_http(cfg.health_port, SERVICE_NAME, ready_check=_ready)

    grpc_server = GrpcServer(service_name=SERVICE_NAME, port=cfg.grpc_port)
    await grpc_server.start()

    logger.info("loading ASR models (this can take a while on first run)")
    bundle = await asyncio.to_thread(load_models, asr_cfg)
    holder["bundle"] = bundle

    worker = StageWorker(
        service_name=SERVICE_NAME,
        redis=redis,
        storage=storage,
        stream=STREAM_ASR,
        group=GROUP_ASR_WORKERS,
        handler=build_asr_handler(bundle),
        # Sequential by default: faster-whisper/pyannote are CPU-bound and
        # already use multiple threads internally per call, so running
        # several jobs concurrently would thrash rather than parallelise.
        concurrency=1,
        batch=2,
        # Must exceed real worst-case STAGE_ASR wall-clock, or the periodic
        # reclaim sweep (coda_worker_sdk.consumer.StreamConsumer, 30s cadence)
        # self-reclaims a message that is still being legitimately handled by
        # this same in-flight task, dispatching a duplicate run of the same
        # job the moment the original finishes — and if that duplicate also
        # exceeds the threshold, the cycle repeats forever, starving every
        # other queued job (found live: a ~7.6-minute PriMock57 clip took
        # 569,922ms end-to-end, comfortably over the SDK's 5-minute default).
        # 40 minutes matches go/internal/queue/policy.go's
        # PolicyFor(STAGE_ASR, LocalASR).VisibilityTimeout exactly — the two
        # must not drift independently.
        min_idle_ms=40 * 60 * 1000,
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    worker_task = asyncio.ensure_future(worker.run())
    logger.info(
        "listening",
        extra={
            "extra_fields": {
                "grpc_port": cfg.grpc_port,
                "health_port": cfg.health_port,
                "stream": STREAM_ASR,
                "model_size": asr_cfg.model_size,
                "device": asr_cfg.device,
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
