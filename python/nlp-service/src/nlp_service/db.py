"""nlp-service's Postgres access. Extends architecture.md §1.2's original
"LLM response cache only" scope to also write `extractions`/`summaries`/
`clinical_notes` (claude_context.md decision #66) — nlp-service is the
service that computes these values, and nothing else was ever wired to
persist them (decision #53 flagged `clinical_notes` specifically as
unpopulated).

Real Postgres access also gives nlp-service a `RunConfig` fetch-by-id path
that decision #58 said asr-service still lacks — `fetch_run_config` resolves
that for this service only.

All functions take an already-checked-out `psycopg.AsyncConnection` (from
`coda_worker_sdk.PostgresPool.connection()`), never the pool itself, so
callers control transaction boundaries explicitly.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import psycopg
from google.protobuf import json_format
from psycopg.rows import dict_row

from coda.v1 import clinical_pb2, runconfig_pb2
from nlp_service.llm.client import LLMCompletion

# Mirrors go/migrations/000019_extractions.up.sql's field_key CHECK — the 8
# clinical fields from claude_context.md §3, with medications/allergies
# split into two rows (the taxonomy's one object-typed field becomes two
# extraction rows, matching the DB constraint the Go side already shipped).
FIELD_KEYS = (
    "chief_complaint",
    "hopi",
    "past_medical_history",
    "medications",
    "allergies",
    "examination_findings",
    "provisional_diagnosis",
    "investigations_advised",
    "treatment_plan",
)


@dataclass(frozen=True, slots=True)
class RunConfigRow:
    id: str
    arm: str
    config: runconfig_pb2.RunConfig


async def fetch_run_config(conn: psycopg.AsyncConnection, run_config_id: str) -> RunConfigRow:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, arm, config FROM run_configs WHERE id = %s", (run_config_id,)
        )
        row = await cur.fetchone()
    if row is None:
        raise LookupError(f"nlp_service.db: no run_configs row for id={run_config_id!r}")
    cfg = runconfig_pb2.RunConfig()
    json_format.ParseDict(row["config"], cfg, ignore_unknown_fields=True)
    return RunConfigRow(id=str(row["id"]), arm=row["arm"], config=cfg)


def prompt_sha256(system_prompt: str, user_prompt: str) -> str:
    h = hashlib.sha256()
    h.update(system_prompt.encode("utf-8"))
    h.update(b"\x00")
    h.update(user_prompt.encode("utf-8"))
    return h.hexdigest()


async def get_cached_completion(
    conn: psycopg.AsyncConnection, *, model: str, sha256: str
) -> LLMCompletion | None:
    """Cache lookup (architecture.md §5.5's `llm_cache`, decision #28). On
    hit, increments `hit_count` in the same round trip and returns a
    completion reconstructed from the cached response — the caller never
    re-calls the LLM.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            UPDATE llm_cache SET hit_count = hit_count + 1
            WHERE model = %s AND prompt_sha256 = %s
            RETURNING response, tokens_in, tokens_out
            """,
            (model, sha256),
        )
        row = await cur.fetchone()
    await conn.commit()
    if row is None:
        return None
    response = row["response"]
    content = response["content"] if isinstance(response, dict) else str(response)
    return LLMCompletion(
        content=content,
        tokens_in=row["tokens_in"],
        tokens_out=row["tokens_out"],
        model=model,
        latency_ms=0,
        cost_estimate=0.0,
    )


async def store_completion(
    conn: psycopg.AsyncConnection, *, model: str, sha256: str, completion: LLMCompletion
) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO llm_cache (model, prompt_sha256, response, tokens_in, tokens_out)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (model, prompt_sha256) DO NOTHING
            """,
            (
                model,
                sha256,
                json.dumps({"content": completion.content}),
                completion.tokens_in,
                completion.tokens_out,
            ),
        )
    await conn.commit()


async def write_pipeline_outputs(
    conn: psycopg.AsyncConnection,
    *,
    consultation_id: str,
    run_config_id: str,
    note: clinical_pb2.ClinicalNote,
    summary_text: str,
) -> None:
    """Writes `extractions` (9 rows), `summaries` (1 row), and
    `clinical_notes` (1 row, version=1, status='draft') in one transaction —
    a partial write across these three never happens.
    """
    field_values: dict[str, list[clinical_pb2.FieldValue]] = {
        "chief_complaint": [note.chief_complaint] if note.HasField("chief_complaint") else [],
        "hopi": [note.hopi] if note.HasField("hopi") else [],
        "past_medical_history": list(note.past_medical_history),
        "medications": list(note.medications_allergies.medications),
        "allergies": list(note.medications_allergies.allergies),
        "examination_findings": (
            [note.examination_findings] if note.HasField("examination_findings") else []
        ),
        "provisional_diagnosis": list(note.provisional_diagnosis),
        "investigations_advised": list(note.investigations_advised),
        "treatment_plan": [note.treatment_plan] if note.HasField("treatment_plan") else [],
    }

    async with conn.transaction(), conn.cursor() as cur:
        for field_key in FIELD_KEYS:
            values = field_values[field_key]
            if not values:
                # No supported content for this field on this
                # consultation — no row, not a row with an empty value
                # (§3: absent information is null, never a fabricated
                # empty-but-present record).
                continue
            for value in values:
                await cur.execute(
                    """
                        INSERT INTO extractions
                            (consultation_id, run_config_id, field_key, value, iteration)
                        VALUES (%s, %s, %s, %s, 0)
                        ON CONFLICT (consultation_id, run_config_id, field_key, iteration)
                        DO UPDATE SET value = EXCLUDED.value
                        """,
                    (
                        consultation_id,
                        run_config_id,
                        field_key,
                        json_format.MessageToJson(value, preserving_proto_field_name=True),
                    ),
                )

        await cur.execute(
            """
                INSERT INTO summaries (consultation_id, run_config_id, text)
                VALUES (%s, %s, %s)
                ON CONFLICT (consultation_id, run_config_id) DO UPDATE SET text = EXCLUDED.text
                """,
            (consultation_id, run_config_id, summary_text),
        )

        await cur.execute(
            """
                INSERT INTO clinical_notes (consultation_id, run_config_id, version, status, note)
                VALUES (%s, %s, 1, 'draft', %s)
                ON CONFLICT (consultation_id, run_config_id, version)
                DO UPDATE SET note = EXCLUDED.note
                """,
            (
                consultation_id,
                run_config_id,
                json_format.MessageToJson(note, preserving_proto_field_name=True),
            ),
        )


__all__ = [
    "FIELD_KEYS",
    "RunConfigRow",
    "fetch_run_config",
    "prompt_sha256",
    "get_cached_completion",
    "store_completion",
    "write_pipeline_outputs",
]
