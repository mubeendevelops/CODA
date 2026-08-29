"""A small async gRPC server harness for the synchronous endpoints
architecture.md §2.6 defines — Health/Ready, GetModelInfo, RegenerateField,
EmbedTexts. gRPC is explicitly scoped to these bounded, fast calls and never
to pipeline stages (ADR-0003); the Streams transport in consumer.py/worker.py
is the stage path.

Uses `grpc.aio` so the gRPC server and the asyncio stage-consumer loop share
one event loop in the same process, rather than needing a second thread.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

logger = logging.getLogger(__name__)

ServiceRegistrar = Callable[[grpc.aio.Server], None]
"""A `add_XServicer_to_server(servicer, server)` call, generated per-service
by protoc. Passed in by the caller since the harness itself knows nothing
about any particular service's RPCs."""


class GrpcServer:
    """Wraps `grpc.aio.server`, always carrying the standard health service
    (architecture.md §2.6's `Health`/`Ready` call) plus whatever the caller
    registers via `add_service`.
    """

    def __init__(self, *, service_name: str, port: int, max_workers: int = 8) -> None:
        self._service_name = service_name
        self._port = port
        self._server = grpc.aio.server()
        self._health = health.aio.HealthServicer()
        health_pb2_grpc.add_HealthServicer_to_server(self._health, self._server)
        self._server.add_insecure_port(f"0.0.0.0:{port}")  # noqa: S104 - internal network only

    @property
    def server(self) -> grpc.aio.Server:
        return self._server

    def add_service(self, registrar: ServiceRegistrar) -> None:
        """Registers one more service on this server. Must be called before
        `start()`.
        """
        registrar(self._server)

    async def set_serving(self, serving: bool, *, service: str = "") -> None:
        status = (
            health_pb2.HealthCheckResponse.SERVING
            if serving
            else health_pb2.HealthCheckResponse.NOT_SERVING
        )
        await self._health.set(service, status)

    async def start(self) -> None:
        await self._health.set(self._service_name, health_pb2.HealthCheckResponse.SERVING)
        await self._health.set("", health_pb2.HealthCheckResponse.SERVING)
        await self._server.start()
        logger.info("grpc server listening", extra={"extra_fields": {"port": self._port}})

    async def wait_for_termination(self) -> None:
        await self._server.wait_for_termination()

    async def stop(self, grace_seconds: float = 10.0) -> None:
        await self._health.set(self._service_name, health_pb2.HealthCheckResponse.NOT_SERVING)
        await self._server.stop(grace_seconds)

    async def serve_until(self, stop_event: asyncio.Event) -> None:
        """Runs until `stop_event` is set, then shuts down gracefully.
        Convenience for the common "start, wait for a signal, stop" flow.
        """
        await self.start()
        await stop_event.wait()
        await self.stop()


__all__ = ["GrpcServer", "ServiceRegistrar"]
