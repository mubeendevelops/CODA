"""Persistence for Module 5/6 — every candidate, every score, every
iteration.

The discarded candidates are the point. A case-study figure showing "the
graph arm considered these three and picked that one, for these scores" is
only possible if the two it rejected were stored, and a score trajectory is
only possible if each iteration was. So this writes:

- one `extractions` row per (field, value_index) for the FINAL iteration,
  carrying the selected value, its `score_breakdown`, the non-selecting
  backend's `secondary_score_breakdown` when `scorer_backend = both`,
  per-field token accounting, and a `candidate_set_uri` pointing at the full
  trace. The `iteration` column records which iteration that value came from,
  so the table says how much refinement the delivered value had — it does not
  hold a row per intermediate iteration;
- one `RefinementTrace` artifact per field, holding every candidate and every
  score at every iteration, including the losers.

**Where each question is answered.** "What is the note, and what did each
field cost?" is a `GROUP BY` over `extractions`. "How did this field get
there, and what was rejected on the way?" is the trace artifact. The
per-iteration history deliberately lives only in the artifact: `extractions`
is the ablation's join table and one row per field per run keeps it that way,
while a K=2 run over nine fields would otherwise triple it with rows no
comparison query reads. `RefinementTrace.score_trajectory` carries the
trajectory itself, so nothing the figures need is lost.

`value_index` is why migration 000033 exists: `extractions` previously had
UNIQUE (consultation, run_config, field_key, iteration), so a list field's
items all collided on one key and only the last survived (a silent Phase 4
bug, fixed there).
"""

from __future__ import annotations

import json
import logging

import psycopg
from google.protobuf import json_format

from coda.v1 import clinical_pb2, got_pb2
from nlp_service.reasoning.generation import LIST_FIELDS
from nlp_service.reasoning.refine import FieldOutcome

logger = logging.getLogger(__name__)


def build_trace(
    *,
    consultation_id: str,
    run_config_id: str,
    field_key: str,
    iterations: list[got_pb2.CandidateSet],
    final_value: clinical_pb2.FieldValue,
    trajectory: list[float],
    scorer_backend: str,
    tokens_in: int,
    tokens_out: int,
    llm_calls: int,
    cache_hits: int,
) -> got_pb2.RefinementTrace:
    return got_pb2.RefinementTrace(
        consultation_id=consultation_id,
        run_config_id=run_config_id,
        iterations=iterations,
        final_value=final_value,
        score_trajectory=trajectory,
        scorer_backend=scorer_backend,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        llm_calls=llm_calls,
        cache_hits=cache_hits,
    )


def serialize_trace(trace: got_pb2.RefinementTrace) -> bytes:
    return json_format.MessageToJson(
        trace, preserving_proto_field_name=True, indent=None
    ).encode()


def deserialize_trace(raw: bytes) -> got_pb2.RefinementTrace:
    trace = got_pb2.RefinementTrace()
    json_format.Parse(raw, trace, ignore_unknown_fields=True)
    return trace


async def write_reasoning_outputs(
    conn: psycopg.AsyncConnection,
    *,
    consultation_id: str,
    run_config_id: str,
    note: clinical_pb2.ClinicalNote,
    summary_text: str,
    outcomes: dict[str, FieldOutcome],
    trace_uris: dict[str, str],
) -> None:
    """Writes `extractions` (all fields, all iterations), `summaries`, and
    `clinical_notes` in one transaction — a partial write across the three
    never happens.

    Existing rows for this (consultation, run_config) are deleted first
    rather than upserted. Iteration and value_index counts vary between runs
    (a re-run with different K produces a different number of rows), so an
    upsert would leave a previous run's orphaned rows behind and silently
    blend two runs' traces.
    """
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM extractions WHERE consultation_id = %s AND run_config_id = %s",
            (consultation_id, run_config_id),
        )

        for field_key, outcome in outcomes.items():
            values = _values_for(field_key, outcome)
            score_json = json_format.MessageToJson(
                outcome.score, preserving_proto_field_name=True
            )
            secondary_json = (
                json_format.MessageToJson(
                    outcome.secondary_score, preserving_proto_field_name=True
                )
                if outcome.secondary_score is not None
                else None
            )
            for value_index, text in enumerate(values):
                await cur.execute(
                    """
                    INSERT INTO extractions
                        (consultation_id, run_config_id, field_key, value,
                         candidate_set_uri, selected_candidate_idx, score_breakdown,
                         secondary_score_breakdown, iteration, value_index,
                         tokens_in, tokens_out, llm_calls, cache_hits)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        consultation_id,
                        run_config_id,
                        field_key,
                        json.dumps(
                            {
                                "value": text,
                                "source_turn_ids": outcome.turn_ids,
                                "confidence": outcome.confidence,
                            }
                        ),
                        trace_uris.get(field_key),
                        int(outcome.selected.index),
                        score_json,
                        secondary_json,
                        len(outcome.trace.score_trajectory) - 1
                        if outcome.trace.score_trajectory
                        else 0,
                        value_index,
                        outcome.tokens_in,
                        outcome.tokens_out,
                        outcome.llm_calls,
                        outcome.cache_hits,
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

    logger.info(
        "reasoning outputs persisted",
        extra={
            "extra_fields": {
                "consultation_id": consultation_id,
                "run_config_id": run_config_id,
                "fields": len(outcomes),
                "traces": len(trace_uris),
            }
        },
    )


def _values_for(field_key: str, outcome: FieldOutcome) -> list[str]:
    if field_key in LIST_FIELDS:
        return [i.strip() for i in outcome.selected.items if i.strip()]
    text = outcome.selected.text.strip()
    return [text] if text else []


__all__ = [
    "build_trace",
    "serialize_trace",
    "deserialize_trace",
    "write_reasoning_outputs",
]
