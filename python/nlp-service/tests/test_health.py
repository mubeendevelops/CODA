import json
import urllib.request

from coda_worker_sdk.metrics import serve_http


def test_healthz_returns_ok() -> None:
    server, _thread = serve_http(0, "nlp-service")
    port = server.server_address[1]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as resp:
            body = json.loads(resp.read())
        assert body["status"] == "ok"
        assert body["service"] == "nlp-service"
    finally:
        server.shutdown()
