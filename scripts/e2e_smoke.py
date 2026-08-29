#!/usr/bin/env python3
"""End-to-end walking-skeleton smoke test (plan.md Phase 3 / claude_context.md
decision-in-progress: echo workers). Proves the plumbing — go-api, Postgres,
Redis Streams, go-orchestrator, MinIO, and the Python echo workers — works
end to end, independently of model behaviour: no real ASR or NLP runs here.

Flow: log in -> create a consultation -> presign + upload a fixture WAV ->
confirm the upload -> submit a job -> poll job status -> fetch the result.
Exits 0 and prints a summary on success; exits 1 with a diagnostic on any
failure or timeout.

Stdlib only, deliberately — this must run against a bare `docker compose up`
without a Python environment being set up first.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from pathlib import Path

API_BASE = os.environ.get("E2E_API_BASE", "http://localhost:8080/v1")
EMAIL = os.environ.get("E2E_EMAIL", "doctor@coda.dev")
PASSWORD = os.environ.get("E2E_PASSWORD", "coda-dev-password")
FIXTURE = Path(os.environ.get("E2E_FIXTURE", str(Path(__file__).parent / "fixtures" / "sample_consultation.wav")))
POLL_INTERVAL_SECONDS = 2.0
POLL_TIMEOUT_SECONDS = float(os.environ.get("E2E_TIMEOUT_SECONDS", "120"))

# go-api presigns PUT URLs against MINIO_ENDPOINT (docker-compose.yml:
# "minio:9000"), the container-network hostname. That host is baked into
# the SigV4 signature itself, so a client outside the compose network
# cannot rewrite the URL to something it can reach — any host:port swap
# invalidates the signature. `make e2e` therefore runs this script as the
# `e2e` service in docker-compose.yml (`docker compose run --rm e2e`),
# inside the compose network, where "minio:9000" resolves directly and no
# rewrite is needed. E2E_MINIO_REWRITE_TO exists only for a deployment
# where MINIO_ENDPOINT itself is already a host-reachable address.
MINIO_INTERNAL_NETLOC = os.environ.get("E2E_MINIO_INTERNAL_NETLOC", "minio:9000")
MINIO_HOST_NETLOC = os.environ.get("E2E_MINIO_REWRITE_TO", "")


def _rewrite_to_host_reachable_url(url: str) -> str:
    if not MINIO_HOST_NETLOC:
        return url
    parts = urllib.parse.urlsplit(url)
    if parts.netloc != MINIO_INTERNAL_NETLOC:
        return url
    return urllib.parse.urlunsplit(parts._replace(netloc=MINIO_HOST_NETLOC))

TERMINAL_SUCCESS_STATES = {"awaiting_review", "under_review", "approved", "exported"}
TERMINAL_FAILURE_STATES = {"failed", "dead_lettered", "cancelled"}


class SmokeTestError(RuntimeError):
    pass


def _request(method: str, path: str, *, token: str | None = None, body: dict | None = None,
             raw_body: bytes | None = None, content_type: str | None = None) -> tuple[int, dict | bytes]:
    url = path if path.startswith("http") else f"{API_BASE}{path}"
    data: bytes | None = None
    headers: dict[str, str] = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    elif raw_body is not None:
        data = raw_body
        if content_type:
            headers["Content-Type"] = content_type
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code

    if raw:
        try:
            return status, json.loads(raw)
        except json.JSONDecodeError:
            return status, raw
    return status, {}


def _step(label: str) -> None:
    print(f"--> {label}", flush=True)


def login() -> str:
    _step(f"logging in as {EMAIL}")
    status, resp = _request("POST", "/auth/login", body={"email": EMAIL, "password": PASSWORD})
    if status != 200 or not isinstance(resp, dict) or "access_token" not in resp:
        raise SmokeTestError(f"login failed: {status} {resp!r}")
    return resp["access_token"]


def create_consultation(token: str) -> str:
    _step("creating consultation")
    status, resp = _request(
        "POST",
        "/consultations",
        token=token,
        body={
            "language": "en",
            "consent": {"consent_obtained": True, "consent_type": "verbal"},
        },
    )
    if status != 201 or not isinstance(resp, dict) or "id" not in resp:
        raise SmokeTestError(f"create consultation failed: {status} {resp!r}")
    return resp["id"]


def wav_duration_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def upload_audio(token: str, consultation_id: str) -> None:
    data = FIXTURE.read_bytes()
    sha256 = hashlib.sha256(data).hexdigest()

    _step(f"presigning audio upload ({len(data)} bytes, sha256={sha256[:12]}...)")
    status, resp = _request(
        "POST",
        f"/consultations/{consultation_id}/audio/presign",
        token=token,
        body={"content_type": "audio/wav", "size_bytes": len(data), "sha256": sha256},
    )
    if status != 200 or not isinstance(resp, dict) or "upload_url" not in resp:
        raise SmokeTestError(f"presign failed: {status} {resp!r}")
    upload_url = _rewrite_to_host_reachable_url(resp["upload_url"])
    object_key = resp["object_key"]

    _step("uploading fixture WAV directly to object storage")
    status, resp = _request("PUT", upload_url, raw_body=data, content_type="audio/wav")
    if status not in (200, 204):
        raise SmokeTestError(f"presigned PUT failed: {status} {resp!r}")

    _step("confirming upload")
    status, resp = _request(
        "POST",
        f"/consultations/{consultation_id}/audio/confirm",
        token=token,
        body={
            "object_key": object_key,
            "sha256": sha256,
            "duration_sec": wav_duration_seconds(FIXTURE),
        },
    )
    if status != 200:
        raise SmokeTestError(f"confirm failed: {status} {resp!r}")


def submit_job(token: str, consultation_id: str) -> str:
    _step("submitting job (arm=baseline)")
    status, resp = _request(
        "POST", f"/consultations/{consultation_id}/jobs", token=token, body={"arm": "baseline"}
    )
    if status != 202 or not isinstance(resp, dict) or "job_id" not in resp:
        raise SmokeTestError(f"submit job failed: {status} {resp!r}")
    return resp["job_id"]


def poll_job(token: str, job_id: str) -> dict:
    _step(f"polling job {job_id} (timeout {POLL_TIMEOUT_SECONDS:.0f}s)")
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    last_state = None
    while time.monotonic() < deadline:
        status, resp = _request("GET", f"/jobs/{job_id}", token=token)
        if status != 200 or not isinstance(resp, dict):
            raise SmokeTestError(f"get job failed: {status} {resp!r}")
        state = resp.get("state")
        if state != last_state:
            print(
                f"    job state={state} stage={resp.get('current_stage')} "
                f"progress={resp.get('progress_percent')}%",
                flush=True,
            )
            last_state = state
        if state in TERMINAL_FAILURE_STATES:
            raise SmokeTestError(f"job reached failure state {state!r}: {resp.get('error')!r}")
        if state in TERMINAL_SUCCESS_STATES:
            return resp
        time.sleep(POLL_INTERVAL_SECONDS)
    raise SmokeTestError(f"timed out after {POLL_TIMEOUT_SECONDS:.0f}s waiting for job to complete")


def fetch_result(token: str, consultation_id: str) -> tuple[int, dict]:
    """A 409 here is expected at this stage of the project, not a failure:
    `clinical_notes` is only populated once real NLP distillation exists
    (plan.md Phase 6) — the echo nlp-service worker writes its dummy
    ClinicalNote to MinIO as a stage artifact (proven by the job reaching
    `awaiting_review`), but nothing yet materialises that into the
    `clinical_notes` table go-api's result endpoint reads from. The walking
    skeleton's job is the pipeline plumbing, proven by `poll_job` above;
    this call still exercises the endpoint and reports what it returns.
    """
    _step("fetching consultation result")
    status, resp = _request("GET", f"/consultations/{consultation_id}/result", token=token)
    if status not in (200, 409) or not isinstance(resp, dict):
        raise SmokeTestError(f"get result failed unexpectedly: {status} {resp!r}")
    return status, resp


def main() -> int:
    if not FIXTURE.exists():
        print(f"fixture WAV not found: {FIXTURE}", file=sys.stderr)
        return 1

    started = time.monotonic()
    try:
        token = login()
        consultation_id = create_consultation(token)
        upload_audio(token, consultation_id)
        job_id = submit_job(token, consultation_id)
        job = poll_job(token, job_id)
        result_status, result = fetch_result(token, consultation_id)
    except SmokeTestError as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        return 1

    elapsed = time.monotonic() - started
    print("\nPASSED — walking skeleton proven end to end (real upload, real Redis")
    print("Streams dispatch, real MinIO artifacts, real orchestrator state machine,")
    print("real echo workers — no real ASR/NLP model ran)")
    print(f"  consultation_id = {consultation_id}")
    print(f"  job_id          = {job_id}")
    print(f"  final job state = {job.get('state')}")
    if result_status == 200:
        print(f"  result state    = {result.get('state')}")
        print(f"  has transcript  = {result.get('transcript') is not None}")
        print(f"  has clinical_note = {result.get('clinical_note') is not None}")
    else:
        print(f"  result endpoint = 409 {result.get('error')!r} (expected — see Phase 6)")
    print(f"  elapsed         = {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
