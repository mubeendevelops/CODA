"""asr-service entrypoint.

Phase 0: serves gRPC + HTTP health only. Transcription (Groq whisper-large-v3
-turbo), pyannote diarization, and transcript assembly land in Phase 1
(docs/architecture.md §1.2 asr-service; this worker must never know about
clinical fields or the thought graph).
"""

from __future__ import annotations

import logging
import os
import signal
import threading
from concurrent import futures
from types import FrameType

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

from asr_service.health import serve_health_http

SERVICE_NAME = "asr-service"


def _configure_logging() -> logging.Logger:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format='{"service": "%(name)s", "level": "%(levelname)s", "message": "%(message)s"}',
    )
    return logging.getLogger(SERVICE_NAME)


def main() -> None:
    logger = _configure_logging()

    grpc_port = int(os.environ.get("GRPC_PORT", "50051"))
    health_port = int(os.environ.get("HEALTH_PORT", "8082"))

    grpc_server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    health_servicer = health.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, grpc_server)
    grpc_server.add_insecure_port(f"0.0.0.0:{grpc_port}")  # noqa: S104 - internal network only
    health_servicer.set(SERVICE_NAME, health_pb2.HealthCheckResponse.SERVING)
    health_servicer.set("", health_pb2.HealthCheckResponse.SERVING)

    http_server = serve_health_http(health_port)
    http_thread = threading.Thread(target=http_server.serve_forever, daemon=True)

    stop_event = threading.Event()

    def _handle_signal(signum: int, _frame: FrameType | None) -> None:
        logger.info("received signal %s, shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    grpc_server.start()
    http_thread.start()
    logger.info("listening: grpc=:%d health=:%d", grpc_port, health_port)

    stop_event.wait()
    health_servicer.set(SERVICE_NAME, health_pb2.HealthCheckResponse.NOT_SERVING)
    grpc_server.stop(grace=10)
    http_server.shutdown()


if __name__ == "__main__":
    main()
