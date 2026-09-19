"""Serialization and reconstruction of the thought graph.

The requirement these cover is "make the whole graph reconstructible for the
report figures". Two reconstruction paths exist and both are tested:
protojson round-trip through the artifact, and rebuild from `thoughts` /
`thought_edges` rows. The SQL path is tested against a real Postgres when
`CODA_TEST_DATABASE_URL` is set and skipped otherwise — mocking a cursor into
agreeing with the query it was handed proves nothing about whether the schema
accepts the write.
"""

from __future__ import annotations

import json
import os
import pathlib

import pytest
from fixtures import transcripts

from coda.v1 import thought_pb2
from nlp_service.graph import store
from nlp_service.graph.edges import assemble_edges
from nlp_service.graph.thoughts import construct_thoughts
from nlp_service.llm.cassette import CassetteLLMClient, CassetteTurn

CASSETTES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "cassettes"

Cat = thought_pb2.ThoughtCategory
Pol = thought_pb2.Polarity
EdgeT = thought_pb2.EdgeType


async def build(name: str, factory, conn):  # type: ignore[no-untyped-def]
    transcript = factory()
    data = json.loads((CASSETTES / f"{name}.json").read_text())
    client = CassetteLLMClient(turns=[CassetteTurn(**t) for t in data])
    construction = await construct_thoughts(
        conn=conn,
        llm_client=client,
        model="qwen/qwen3.8-27b",
        transcript=transcript,
        consultation_id="c-fixture",
        run_config_id="rc-fixture",
        turn_id_by_index={t.turn_index: f"uuid-turn-{t.turn_index}" for t in transcript.turns},
        repair_max_attempts=2,
        timeout_s=5.0,
    )
    assembly = await assemble_edges(
        conn=conn,
        llm_client=client,
        model="qwen/qwen3.6-27b",
        thoughts=construction.thoughts,
        consultation_id="c-fixture",
        run_config_id="rc-fixture",
        repair_max_attempts=2,
        timeout_s=5.0,
    )
    return transcript, construction.thoughts, assembly.edges


async def test_graph_round_trips_through_the_artifact(fake_conn: object) -> None:
    """A stored artifact must deserialize into the same message a figure
    script can walk, years later, with no database available.
    """
    transcript, thoughts, edges = await build(
        "negation", transcripts.negation_transcript, fake_conn
    )
    graph = store.build_graph(
        consultation_id="c-fixture",
        run_config_id="rc-fixture",
        thoughts=thoughts,
        edges=edges,
        prompt_set_hash="deadbeef",
        language="en",
        construction_model="qwen/qwen3.8-27b",
        edge_model="qwen/qwen3.6-27b",
        turn_count=len(transcript.turns),
    )
    restored = store.deserialize_graph(store.serialize_graph(graph))
    assert restored == graph


async def test_artifact_is_self_describing(fake_conn: object) -> None:
    """Provenance travels with the graph, so a figure never has to re-join
    `run_configs` to say what produced it."""
    transcript, thoughts, edges = await build(
        "disfluency", transcripts.disfluency_transcript, fake_conn
    )
    graph = store.build_graph(
        consultation_id="c-fixture",
        run_config_id="rc-fixture",
        thoughts=thoughts,
        edges=edges,
        prompt_set_hash="abc123",
        language="en",
        construction_model="qwen/qwen3.8-27b",
        edge_model="qwen/qwen3.6-27b",
        turn_count=len(transcript.turns),
    )
    payload = json.loads(store.serialize_graph(graph))
    assert payload["prompt_set_hash"] == "abc123"
    assert payload["construction_model"] == "qwen/qwen3.8-27b"
    assert payload["edge_model"] == "qwen/qwen3.6-27b"
    assert payload["language"] == "en"
    assert payload["turn_count"] == len(transcript.turns)
    # Field names, not lowerCamelCase — every other artifact this system
    # writes uses preserving_proto_field_name, and one reader handles them all.
    assert "src_thought_id" in payload["edges"][0]
    assert "text_span" not in payload["thoughts"][0]  # spans are offsets, not text
    assert "char_start" in payload["thoughts"][0]


async def test_polarity_and_span_survive_serialization(fake_conn: object) -> None:
    """The two fields added for dialogue are exactly the ones a naive
    round-trip would drop, since both are proto3 scalars whose default is
    falsy."""
    _, thoughts, edges = await build("negation", transcripts.negation_transcript, fake_conn)
    graph = store.build_graph(
        consultation_id="c",
        run_config_id="rc",
        thoughts=thoughts,
        edges=edges,
        prompt_set_hash="",
        language="en",
        construction_model="m",
        edge_model="m",
        turn_count=11,
    )
    restored = store.deserialize_graph(store.serialize_graph(graph))
    negated = [t for t in restored.thoughts if t.polarity == Pol.POLARITY_NEGATED]
    assert negated
    for t in restored.thoughts:
        assert t.char_end > t.char_start
    assert any(e.edge_type == EdgeT.EDGE_TYPE_NEGATION for e in restored.edges)


def test_apply_id_map_rewrites_both_endpoints() -> None:
    """Build-local `t{n}` handles become database UUIDs, so the artifact and
    the database name the same nodes."""
    thoughts = [
        thought_pb2.Thought(id="t1", turn_index=0),
        thought_pb2.Thought(id="t2", turn_index=1),
    ]
    edges = [thought_pb2.ThoughtEdge(src_thought_id="t1", dst_thought_id="t2")]
    mapped_t, mapped_e = store.apply_id_map(thoughts, edges, {"t1": "uuid-a", "t2": "uuid-b"})
    assert [t.id for t in mapped_t] == ["uuid-a", "uuid-b"]
    assert (mapped_e[0].src_thought_id, mapped_e[0].dst_thought_id) == ("uuid-a", "uuid-b")
    # The originals are untouched — callers still hold usable build-local ids.
    assert thoughts[0].id == "t1"


def test_apply_id_map_drops_edges_with_an_unmapped_endpoint() -> None:
    """An edge naming a thought that was dropped before persistence carries
    no information and must not reach the artifact."""
    thoughts = [thought_pb2.Thought(id="t1")]
    edges = [thought_pb2.ThoughtEdge(src_thought_id="t1", dst_thought_id="t9")]
    _, mapped_e = store.apply_id_map(thoughts, edges, {"t1": "uuid-a"})
    assert mapped_e == []


# --------------------------------------------------------------------------
# The SQL path — real Postgres only
# --------------------------------------------------------------------------

DB_URL = os.environ.get("CODA_TEST_DATABASE_URL", "")

pytestmark_db = pytest.mark.skipif(
    not DB_URL,
    reason="set CODA_TEST_DATABASE_URL to a migrated Postgres to run the persistence tests",
)


@pytestmark_db
async def test_persist_and_load_round_trip_through_postgres(fake_conn: object) -> None:
    """architecture.md §6's reproducibility guarantee, made executable for
    the graph: if `load_graph` can rebuild the figure source from rows alone,
    the graph is reproducible from the database and not only from a
    surviving artifact file.

    Requires a migrated database with an `orgs`/`consultations`/`run_configs`
    row to hang the foreign keys off; the fixture below creates them.
    """
    import psycopg

    _, thoughts, edges = await build("negation", transcripts.negation_transcript, fake_conn)

    async with await psycopg.AsyncConnection.connect(DB_URL) as conn:
        async with conn.cursor() as cur:
            await cur.execute("INSERT INTO orgs (name, region) VALUES ('t', 'in') RETURNING id")
            org = (await cur.fetchone())[0]  # type: ignore[index]
            await cur.execute(
                """
                INSERT INTO users (org_id, email, password_hash, role)
                VALUES (%s, 'graph-test-' || gen_random_uuid() || '@example.test', 'x', 'doctor')
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

        transcript = transcripts.negation_transcript()
        materialized = await store.materialize_transcript(
            conn,
            transcript=transcript,
            consultation_id=consultation_id,
            run_config_id=run_config_id,
            uri="dev/consultations/x/stages/redact/y/transcript.json",
            asr_backend="faster_whisper_local",
            asr_model="medium",
        )
        assert len(materialized.turn_id_by_index) == len(transcript.turns)

        # Idempotent under redelivery (architecture.md §2.3) — the same turn
        # UUIDs come back, not a second set.
        again = await store.materialize_transcript(
            conn,
            transcript=transcript,
            consultation_id=consultation_id,
            run_config_id=run_config_id,
            uri="dev/consultations/x/stages/redact/y/transcript.json",
            asr_backend="faster_whisper_local",
            asr_model="medium",
        )
        assert again.turn_id_by_index == materialized.turn_id_by_index

        for t in thoughts:
            t.consultation_id = consultation_id
            t.run_config_id = run_config_id
            t.turn_id = materialized.turn_id_by_index[t.turn_index]
        for e in edges:
            e.consultation_id = consultation_id
            e.run_config_id = run_config_id

        id_map = await store.persist_graph(
            conn,
            consultation_id=consultation_id,
            run_config_id=run_config_id,
            thoughts=thoughts,
            edges=edges,
        )
        assert len(id_map) == len(thoughts)

        loaded = await store.load_graph(
            conn, consultation_id=consultation_id, run_config_id=run_config_id
        )
        assert len(loaded.thoughts) == len(thoughts)
        assert len(loaded.edges) == len(edges)

        # The dialogue-specific columns survive the schema, which is what
        # migration 000032 exists for.
        assert any(t.polarity == Pol.POLARITY_NEGATED for t in loaded.thoughts)
        assert any(e.edge_type == EdgeT.EDGE_TYPE_NEGATION for e in loaded.edges)
        assert all(t.char_end > t.char_start for t in loaded.thoughts)
        assert all(t.turn_index >= 0 for t in loaded.thoughts)

        # Re-persisting replaces rather than duplicates.
        await store.persist_graph(
            conn,
            consultation_id=consultation_id,
            run_config_id=run_config_id,
            thoughts=thoughts,
            edges=edges,
        )
        reloaded = await store.load_graph(
            conn, consultation_id=consultation_id, run_config_id=run_config_id
        )
        assert len(reloaded.thoughts) == len(thoughts)
