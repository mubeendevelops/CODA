"""Writes eval results into the real Postgres schema — `run_configs` /
`eval_runs` / `eval_results` (docs/architecture.md §6) — plus the minimal
`orgs`/`users`/`consent_records`/`consultations` rows those results need to
reference, since `eval_results.consultation_id` is a mandatory FK into
`consultations` (dataset items are ingested as first-class consultations,
per claude_context.md decision #3b's "map PriMock57 onto the 8 fields").

This module writes directly to Postgres with `psycopg` rather than through
`sqlc`/the Go layer — `coda_eval` is an offline analysis tool run by a human
on the host, not a pipeline worker, so docs/architecture.md §1.2's
"workers must not touch Postgres directly" doesn't apply to it. It never
writes anywhere the Go/orchestrator side also writes to concurrently
(`jobs`, `job_stages`, pipeline state) — only the eval-specific tables plus
the bootstrap rows above, all idempotent by explicit lookup-before-insert.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

from coda_eval.config import PostgresConfig

_EVAL_ORG_NAME = "CODA Eval Harness"
_EVAL_USER_EMAIL = "eval-harness@coda.local"
_EVAL_USER_ROLE = "auditor"
_EVAL_CONSENT_TYPE = "dataset_licence"


def connect(cfg: PostgresConfig) -> psycopg.Connection:
    return psycopg.connect(cfg.dsn())


def git_sha(repo_root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return out.stdout.strip()
    except Exception:
        return "unknown"


def ensure_eval_org(conn: psycopg.Connection) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM orgs WHERE name = %s", (_EVAL_ORG_NAME,))
        row = cur.fetchone()
        if row:
            return str(row[0])
        cur.execute(
            "INSERT INTO orgs (name, region, settings) VALUES (%s, %s, %s) RETURNING id",
            (_EVAL_ORG_NAME, "eval", json.dumps({"purpose": "coda_eval dataset ingestion"})),
        )
        return str(cur.fetchone()[0])  # type: ignore[index]


def ensure_eval_user(conn: psycopg.Connection, org_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE email = %s", (_EVAL_USER_EMAIL,))
        row = cur.fetchone()
        if row:
            return str(row[0])
        cur.execute(
            "INSERT INTO users (org_id, email, password_hash, role) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (org_id, _EVAL_USER_EMAIL, "no-login:coda-eval-system-account", _EVAL_USER_ROLE),
        )
        return str(cur.fetchone()[0])  # type: ignore[index]


def ensure_consent_record(
    conn: psycopg.Connection, *, org_id: str, granted_by: str, item_id: str, licence: str
) -> str:
    """`subject_ref = item_id` (pseudonymous, matching §7.2's "never a
    name") doubles as the idempotency key — a dataset item's consent record
    always maps back to the same row, letting `ensure_consultation` find its
    consultation deterministically without a schema change.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM consent_records WHERE subject_ref = %s AND consent_type = %s",
            (item_id, _EVAL_CONSENT_TYPE),
        )
        row = cur.fetchone()
        if row:
            return str(row[0])
        cur.execute(
            "INSERT INTO consent_records (org_id, subject_ref, consent_type, granted_at, "
            "granted_by, scope) VALUES (%s, %s, %s, now(), %s, %s) RETURNING id",
            (org_id, item_id, _EVAL_CONSENT_TYPE, granted_by, json.dumps({"licence": licence})),
        )
        return str(cur.fetchone()[0])  # type: ignore[index]


def ensure_consultation(
    conn: psycopg.Connection,
    *,
    org_id: str,
    owner_user_id: str,
    consent_record_id: str,
    language: str,
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM consultations WHERE consent_record_id = %s", (consent_record_id,)
        )
        row = cur.fetchone()
        if row:
            return str(row[0])
        cur.execute(
            "INSERT INTO consultations (org_id, owner_user_id, consent_record_id, "
            "consent_obtained, consent_method, consent_recorded_at, language) "
            "VALUES (%s, %s, %s, true, %s, now(), %s) RETURNING id",
            (org_id, owner_user_id, consent_record_id, "dataset_licence", language),
        )
        return str(cur.fetchone()[0])  # type: ignore[index]


def canonical_content_hash(config: dict[str, Any]) -> str:
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def intern_run_config(
    conn: psycopg.Connection, *, config: dict[str, Any], arm: str, schema_version: int
) -> str:
    content_hash = canonical_content_hash(config)
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM run_configs WHERE content_hash = %s", (content_hash,))
        row = cur.fetchone()
        if row:
            return str(row[0])
        cur.execute(
            "INSERT INTO run_configs (content_hash, arm, config, schema_version) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (content_hash, arm, json.dumps(config), schema_version),
        )
        return str(cur.fetchone()[0])  # type: ignore[index]


def create_eval_run(
    conn: psycopg.Connection, *, name: str, dataset_split: str, arm: str, run_config_id: str
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO eval_runs (name, dataset_split, arm, run_config_id, started_at, status) "
            "VALUES (%s, %s, %s, %s, now(), 'running') RETURNING id",
            (name, dataset_split, arm, run_config_id),
        )
        return str(cur.fetchone()[0])  # type: ignore[index]


def finish_eval_run(
    conn: psycopg.Connection,
    *,
    eval_run_id: str,
    status: str,
    total_tokens: int = 0,
    total_cost_estimate: float = 0.0,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE eval_runs SET finished_at = now(), status = %s, total_tokens = %s, "
            "total_cost_estimate = %s WHERE id = %s",
            (status, total_tokens, total_cost_estimate, eval_run_id),
        )


def write_eval_result(
    conn: psycopg.Connection,
    *,
    eval_run_id: str,
    consultation_id: str,
    run_config_id: str,
    metric_key: str,
    metric_value: float,
    language: str,
    reference_provenance: str,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO eval_results (eval_run_id, consultation_id, run_config_id, metric_key, "
            "metric_value, language, reference_provenance) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                eval_run_id,
                consultation_id,
                run_config_id,
                metric_key,
                metric_value,
                language,
                reference_provenance,
            ),
        )


@dataclass(frozen=True, slots=True)
class ConsultationRef:
    consultation_id: str
    consent_record_id: str


def ensure_dataset_item_consultation(
    conn: psycopg.Connection, *, item_id: str, language: str, licence: str
) -> ConsultationRef:
    org_id = ensure_eval_org(conn)
    user_id = ensure_eval_user(conn, org_id)
    consent_record_id = ensure_consent_record(
        conn, org_id=org_id, granted_by=user_id, item_id=item_id, licence=licence
    )
    consultation_id = ensure_consultation(
        conn,
        org_id=org_id,
        owner_user_id=user_id,
        consent_record_id=consent_record_id,
        language=language,
    )
    return ConsultationRef(consultation_id=consultation_id, consent_record_id=consent_record_id)


__all__ = [
    "ConsultationRef",
    "canonical_content_hash",
    "connect",
    "create_eval_run",
    "ensure_consent_record",
    "ensure_consultation",
    "ensure_dataset_item_consultation",
    "ensure_eval_org",
    "ensure_eval_user",
    "finish_eval_run",
    "git_sha",
    "intern_run_config",
    "write_eval_result",
]
