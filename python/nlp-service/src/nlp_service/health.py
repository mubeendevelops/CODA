"""HTTP health endpoint, separate from the gRPC health service.

Docker Compose healthchecks shell out to `curl`, which is far simpler against
a plain HTTP endpoint than against gRPC health-checking protocol — so both
are exposed. The gRPC one (server.py) is what go-api's RegenerateField calls
and orchestrator's Health/Ready calls use in later phases (architecture.md §2.6).
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVICE_NAME = "nlp-service"


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib method name
        if self.path not in ("/healthz", "/readyz"):
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps({"service": SERVICE_NAME, "status": "ok"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # keep health-check polling out of the logs


def serve_health_http(port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)  # noqa: S104 - internal network only
    return server
