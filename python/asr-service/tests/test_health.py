import json
import urllib.request

from coda_worker_sdk.metrics import serve_http


def test_healthz_returns_ok() -> None:
    server, _thread = serve_http(0, "asr-service")
    port = server.server_address[1]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as resp:
            body = json.loads(resp.read())
        assert body["status"] == "ok"
        assert body["service"] == "asr-service"
    finally:
        server.shutdown()


def test_metrics_endpoint_serves_prometheus_text() -> None:
    server, _thread = serve_http(0, "asr-service")
    port = server.server_address[1]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=2) as resp:
            assert resp.status == 200
            assert "text/plain" in resp.headers.get("Content-Type", "")
    finally:
        server.shutdown()
