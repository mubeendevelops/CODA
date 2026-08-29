"""Prometheus metrics and the HTTP health/ready/metrics server.

architecture.md §8 names the key series this system tracks; the ones below
are the worker-side half of that list (the orchestrator-side series live in
go/internal/pipeline/metrics.go). Served on the same HTTP port as
/healthz /readyz rather than a dedicated metrics port — asr-service and
nlp-service only ever had one health port allocated (.env.example
ASR_HEALTH_PORT / NLP_HEALTH_PORT), and a Python worker's metrics volume
never justifies a second one.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

STAGE_OUTCOMES_TOTAL = Counter(
    "coda_worker_stage_outcomes_total",
    "Stage results emitted by this worker, by stage and status.",
    ["service", "stage", "status"],
)
STAGE_DURATION_SECONDS = Histogram(
    "coda_worker_stage_duration_seconds",
    "Wall-clock time spent handling one stage envelope, by stage.",
    ["service", "stage"],
)
MESSAGES_RECLAIMED_TOTAL = Counter(
    "coda_worker_messages_reclaimed_total",
    "Entries this worker picked up via XAUTOCLAIM (a previous consumer died mid-message).",
    ["service", "stream"],
)
HEARTBEATS_PUBLISHED_TOTAL = Counter(
    "coda_worker_heartbeats_published_total",
    "StageHeartbeat messages published.",
    ["service", "stage"],
)
IN_FLIGHT_MESSAGES = Gauge(
    "coda_worker_in_flight_messages",
    "Messages currently being handled, bounded by the worker's concurrency limit.",
    ["service"],
)
UNCAUGHT_EXCEPTIONS_TOTAL = Counter(
    "coda_worker_uncaught_exceptions_total",
    "Handler exceptions that fell through the exception boundary unclassified.",
    ["service", "stage"],
)


def _make_handler(
    service_name: str, ready_check: Callable[[], bool]
) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib method name
            if self.path == "/metrics":
                body = generate_latest()
                self.send_response(200)
                self.send_header("Content-Type", CONTENT_TYPE_LATEST)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/readyz":
                ok = ready_check()
                body = json.dumps(
                    {"service": service_name, "status": "ok" if ok else "not_ready"}
                ).encode()
                self.send_response(200 if ok else 503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/healthz":
                body = json.dumps({"service": service_name, "status": "ok"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass  # keep health/metrics polling out of the logs

    return _Handler


def serve_http(
    port: int, service_name: str, *, ready_check: Callable[[], bool] | None = None
) -> tuple[ThreadingHTTPServer, threading.Thread]:
    """Starts /healthz, /readyz, /metrics on a background thread. Returns the
    server (call .shutdown() on it) and the thread it's running on.
    """
    handler_cls = _make_handler(service_name, ready_check or (lambda: True))
    server = ThreadingHTTPServer(("0.0.0.0", port), handler_cls)  # noqa: S104 - internal network only
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


__all__ = [
    "STAGE_OUTCOMES_TOTAL",
    "STAGE_DURATION_SECONDS",
    "MESSAGES_RECLAIMED_TOTAL",
    "HEARTBEATS_PUBLISHED_TOTAL",
    "IN_FLIGHT_MESSAGES",
    "UNCAUGHT_EXCEPTIONS_TOTAL",
    "serve_http",
]
