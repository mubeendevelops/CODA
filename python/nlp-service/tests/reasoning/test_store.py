"""Persistence for Modules 5/6 — every candidate, every score, every
iteration.

The discarded candidates are the point: a case-study figure saying "the graph
arm considered these three and picked that one, for these scores" is only
possible if the two it rejected were stored, and a score trajectory is only
possible if each iteration was.

The SQL half runs against a real Postgres when `CODA_TEST_DATABASE_URL` is
set and is skipped otherwise, matching `tests/graph/test_graph_store.py` —
mocking a cursor into agreeing with the query it was handed proves nothing
about whether the schema accepts the write, and migration 000033's composite
uniqueness key is precisely the kind of thing only a real database enforces.
"""

from __future__ import annotations

import json
import os

import pytest
from factories import candidate, thought

from coda.v1 import clinical_pb2, got_pb2
from nlp_service.reasoning import store
from nlp_service.reasoning.distill import distill_note
from nlp_service.reasoning.refine import FieldOutcome

DB_URL = os.environ.get("CODA_TEST_DATABASE_URL", "")
pytestmark_db = pytest.mark.skipif(
    not DB_URL, reason="set CODA_TEST_DATABASE_URL to a migrated Postgres to run the SQL tests"
)


def outcome(
    field_key: str,
    cand: got_pb2.Candidate,
    *,
    trajectory: list[float] | None = None,
    secondary: got_pb2.ScoreBreakdown | None = None,
    tokens_in: int = 100,
    tokens_out: int = 20,
) -> FieldOutcome:
    oc = FieldOutcome(
        field_key=field_key,
        trace=got_pb2.RefinementTrace(score_trajectory=trajectory or [0.6]),
        selected=cand,
        score=got_pb2.ScoreBreakdown(
            relevance=0.7, consistency=0.6, redundancy=0.1, aggregate=0.61,
            scorer_backend="heuristic",
        ),
        secondary_score=secondary,
        confidence=0.8,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        llm_calls=3,
        cache_hits=1,
    )
    return oc


# ------------------------------------------------------- the trace artifact


def test_a_trace_round_trips_through_its_artifact() -> None:
    """A figure script must be able to read this years later with no database
    available, which is the whole reason the trace is an artifact and not
    only a set of rows."""
    losers = [candidate("weak", index=0), candidate("strong", index=1)]
    trace = store.build_trace(
        consultation_id="c1",
        run_config_id="rc1",
        field_key="chief_complaint",
        iterations=[
            got_pb2.CandidateSet(
                iteration=0,
                candidates=losers,
                scores=[
                    got_pb2.ScoreBreakdown(aggregate=0.2),
                    got_pb2.ScoreBreakdown(aggregate=0.8),
                ],
                selected_index=1,
            )
        ],
        final_value=clinical_pb2.FieldValue(value="strong", source_turn_ids=[3], confidence=0.8),
        trajectory=[0.8, 0.85],
        scorer_backend="heuristic",
        tokens_in=100,
        tokens_out=20,
        llm_calls=3,
        cache_hits=1,
    )
    restored = store.deserialize_trace(store.serialize_trace(trace))
    assert restored == trace


def test_the_trace_keeps_the_candidates_that_lost() -> None:
    """Without this the case-study figure cannot say what was rejected, and
    'the graph arm chose well' becomes an unfalsifiable claim."""
    trace = store.build_trace(
        consultation_id="c1",
        run_config_id="rc1",
        field_key="chief_complaint",
        iterations=[
            got_pb2.CandidateSet(
                iteration=0,
                candidates=[
                    candidate("a", index=0, variant="conservative"),
                    candidate("b", index=1, variant="inclusive"),
                    candidate("c", index=2, variant="negative_aware"),
                ],
                scores=[got_pb2.ScoreBreakdown(aggregate=s) for s in (0.2, 0.8, 0.5)],
                selected_index=1,
            )
        ],
        final_value=clinical_pb2.FieldValue(value="b"),
        trajectory=[0.8],
        scorer_backend="heuristic",
        tokens_in=0, tokens_out=0, llm_calls=0, cache_hits=0,
    )
    payload = json.loads(store.serialize_trace(trace))
    it0 = payload["iterations"][0]
    assert len(it0["candidates"]) == 3
    assert it0["selected_index"] == 1
    assert [c["variant"] for c in it0["candidates"]] == [
        "conservative", "inclusive", "negative_aware",
    ]
    # snake_case, like every other artifact this system writes.
    assert "score_trajectory" in payload


def test_the_trajectory_records_attempts_not_only_adoptions() -> None:
    """A falling trajectory is a real, reportable result (plan.md Phase 6
    AC7). `refine_field`'s regression guard means the delivered value may be
    the earlier one even when the trajectory dips — so the last trajectory
    point is NOT necessarily the final value's score, and anyone drawing the
    figure needs to know that.
    """
    trace = store.build_trace(
        consultation_id="c1",
        run_config_id="rc1",
        field_key="hopi",
        iterations=[],
        final_value=clinical_pb2.FieldValue(value="the better, earlier text"),
        trajectory=[0.80, 0.55],
        scorer_backend="heuristic",
        tokens_in=0, tokens_out=0, llm_calls=0, cache_hits=0,
    )
    assert list(trace.score_trajectory) == [pytest.approx(0.80), pytest.approx(0.55)]
    assert trace.final_value.value == "the better, earlier text"


# ------------------------------------------------------------------ the rows


@pytestmark_db
async def test_reasoning_outputs_persist_and_survive_a_rerun() -> None:
    """The three writes land in one transaction, list items each get their own
    row (migration 000033), and re-running replaces rather than blending."""
    import psycopg

    async with await psycopg.AsyncConnection.connect(DB_URL) as conn:
        async with conn.cursor() as cur:
            await cur.execute("INSERT INTO orgs (name, region) VALUES ('t', 'in') RETURNING id")
            org = (await cur.fetchone())[0]  # type: ignore[index]
            await cur.execute(
                """
                INSERT INTO users (org_id, email, password_hash, role)
                VALUES (%s, 'reason-test-' || gen_random_uuid() || '@example.test', 'x', 'doctor')
                RETURNING id
                """,
                (org,),
            )
            user = (await cur.fetchone())[0]  # type: ignore[index]
            await cur.execute(
                """
                INSERT INTO consent_records
                    (org_id, subject_ref, consent_type, granted_at, granted_by, scope)
                VALUES (%s, 'subj', 'recording', now(), %s, '{}'::jsonb) RETURNING id
                """,
                (org, user),
            )
            consent = (await cur.fetchone())[0]  # type: ignore[index]
            await cur.execute(
                """
                INSERT INTO consultations
                    (org_id, owner_user_id, consent_record_id, state, language)
                VALUES (%s, %s, %s, 'created', 'en') RETURNING id
                """,
                (org, user, consent),
            )
            consultation_id = str((await cur.fetchone())[0])  # type: ignore[index]
            await cur.execute(
                """
                INSERT INTO run_configs (content_hash, arm, config, schema_version)
                VALUES (md5(random()::text), 'got_k2', '{}'::jsonb, 1) RETURNING id
                """
            )
            run_config_id = str((await cur.fetchone())[0])  # type: ignore[index]
        await conn.commit()

        thoughts = [
            thought("t1", 3, "chest pain", ["chest pain"]),
            thought("t2", 7, "diabetes", ["diabetes"]),
        ]
        outcomes = {
            "chief_complaint": outcome(
                "chief_complaint",
                candidate("chest pain", source_thought_ids=["t1"]),
                trajectory=[0.5, 0.61],
                secondary=got_pb2.ScoreBreakdown(aggregate=0.55, scorer_backend="llm_judge"),
            ),
            "past_medical_history": outcome(
                "past_medical_history",
                candidate(
                    "diabetes, hypertension, asthma",
                    items=["diabetes", "hypertension", "asthma"],
                    source_thought_ids=["t2"],
                ),
            ),
        }
        note = distill_note(
            outcomes,
            thoughts=thoughts,
            consultation_id=consultation_id,
            run_config_id=run_config_id,
        )
        await store.write_reasoning_outputs(
            conn,
            consultation_id=consultation_id,
            run_config_id=run_config_id,
            note=note,
            summary_text="A consultation about chest pain.",
            outcomes=outcomes,
            trace_uris={
                "chief_complaint": "dev/c/trace_chief_complaint.json",
                "past_medical_history": "dev/c/trace_past_medical_history.json",
            },
        )

        async with conn.cursor() as cur:
            # Migration 000033: three list items are three rows, not one
            # survivor. This is the bug that migration exists to fix.
            await cur.execute(
                "SELECT value_index, value FROM extractions "
                "WHERE consultation_id = %s AND field_key = 'past_medical_history' "
                "ORDER BY value_index",
                (consultation_id,),
            )
            rows = await cur.fetchall()
            assert [r[0] for r in rows] == [0, 1, 2]
            assert [json.loads(r[1])["value"] if isinstance(r[1], str) else r[1]["value"]
                    for r in rows] == ["diabetes", "hypertension", "asthma"]

            # Provenance reached the row, not just the note.
            await cur.execute(
                "SELECT value, score_breakdown, secondary_score_breakdown, iteration, "
                "       tokens_in, tokens_out, llm_calls, cache_hits, candidate_set_uri "
                "FROM extractions WHERE consultation_id = %s AND field_key = 'chief_complaint'",
                (consultation_id,),
            )
            row = await cur.fetchone()
            assert row is not None
            value = row[0] if isinstance(row[0], dict) else json.loads(row[0])
            assert value["value"] == "chest pain"
            assert value["source_turn_ids"] == [3]
            assert value["confidence"] == pytest.approx(0.8)
            assert row[2] is not None, "scorer_backend=both records the non-selecting score too"
            assert row[3] == 1, "iteration is the last index of the trajectory"
            assert (row[4], row[5], row[6], row[7]) == (100, 20, 3, 1)
            assert row[8] == "dev/c/trace_chief_complaint.json"

            # The other two writes in the same transaction.
            await cur.execute(
                "SELECT text FROM summaries WHERE consultation_id = %s", (consultation_id,)
            )
            assert (await cur.fetchone())[0] == "A consultation about chest pain."  # type: ignore[index]
            await cur.execute(
                "SELECT status FROM clinical_notes WHERE consultation_id = %s", (consultation_id,)
            )
            assert (await cur.fetchone())[0] == "draft"  # type: ignore[index]

        # A re-run with FEWER list items must not leave the old ones behind.
        # An upsert would; the delete-first write does not.
        outcomes["past_medical_history"] = outcome(
            "past_medical_history",
            candidate("diabetes", items=["diabetes"], source_thought_ids=["t2"]),
        )
        await store.write_reasoning_outputs(
            conn,
            consultation_id=consultation_id,
            run_config_id=run_config_id,
            note=note,
            summary_text="Revised summary.",
            outcomes=outcomes,
            trace_uris={},
        )
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*) FROM extractions WHERE consultation_id = %s "
                "AND field_key = 'past_medical_history'",
                (consultation_id,),
            )
            assert (await cur.fetchone())[0] == 1, "the previous run's orphaned rows are gone"  # type: ignore[index]
            await cur.execute(
                "SELECT text FROM summaries WHERE consultation_id = %s", (consultation_id,)
            )
            assert (await cur.fetchone())[0] == "Revised summary."  # type: ignore[index]

        await conn.rollback()
