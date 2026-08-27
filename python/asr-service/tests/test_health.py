import json
import urllib.request

from asr_service.health import serve_health_http


def test_healthz_returns_ok() -> None:
    server = serve_health_http(0)
    port = server.server_address[1]
    try:
        import threading

        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as resp:
            body = json.loads(resp.read())
        assert body["status"] == "ok"
        assert body["service"] == "asr-service"
    finally:
        server.shutdown()
