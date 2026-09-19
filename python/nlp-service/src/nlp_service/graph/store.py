"""Persistence for the thought graph: `thoughts` / `thought_edges` rows, plus
the serialized graph artifact.

Two representations, on purpose, because they answer different questions.

**The database** is the reproducibility guarantee (architecture.md §6: any
reported result must be reconstructible from the database alone). Thoughts
and edges are keyed by `(consultation_id, run_config_id)` exactly like every
other pipeline output, so comparing what the GoT arm saw against what another
arm saw is a `GROUP BY`, not a file-parsing exercise.

**The artifact** is the figure source. A `ThoughtGraph` protojson blob in
object storage carries the whole graph with its provenance (prompt set hash,
models, language, turn count) in one self-describing file, so a case-study
figure can be regenerated from it years later without a live database, and
`docs/` can reference an immutable URI.

## Materializing `transcripts` and `turns`

`thoughts.turn_id` is a foreign key into `turns(id)`, but nothing in this
system had ever written a `turns` row: the sqlc queries for both tables
existed and were called from nowhere, and the transcript lived only as a
MinIO artifact. Thought persistence is therefore blocked until those rows
exist, and this module materializes them from the redacted transcript before
writing any thought.

nlp-service is the right writer for the same reason decision #66 made it the
writer of `extractions`/`summaries`/`clinical_notes`: go-orchestrator is
restricted to metadata-only object-storage reads (architecture.md §1.3) and
so cannot read artifact bytes back to do this, and nlp-service is already
holding the parsed transcript. This extends nlp-service's Postgres scope a
third time, and it is recorded as such.

Writes are idempotent under at-least-once delivery (architecture.md §2.3):
transcripts/turns upsert on their natural keys, and the graph itself is
delete-then-insert within one transaction for its
`(consultation_id, run_config_id)` — a redelivery replaces the graph rather
than doubling it, and a partially-written graph is never observable.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import psycopg
from google.protobuf import json_format
from psycopg.rows import dict_row

from coda.v1 import thought_pb2, transcript_pb2
from nlp_service.graph.edges import EDGE_TYPE_NAME
from nlp_service.graph.thoughts import CATEGORY_NAME, POLARITY_NAME, SPEAKER_NAME

logger = logging.getLogger(__name__)

_PREDICTED_BY_NAME = {
    thought_pb2.PredictedBy.PREDICTED_BY_RULE: "rule",
    thought_pb2.PredictedBy.PREDICTED_BY_LLM: "llm",
}


@dataclass(frozen=True, slots=True)
class MaterializedTranscript:
    transcript_id: str
    turn_id_by_index: dict[int, str]


async def materialize_transcript(
    conn: psycopg.AsyncConnection,
    *,
    transcript: transcript_pb2.Transcript,
    consultation_id: str,
    run_config_id: str,
    uri: str,
    asr_backend: str,
    asr_model: str,
) -> MaterializedTranscript:
    """Upserts one `transcripts` row and its `turns`, returning the turn
    UUIDs keyed by turn_index so thought construction can populate
    `thoughts.turn_id`.

    `asr_backend` must satisfy migration 000015's CHECK
    (`groq` | `faster_whisper_local`). It comes from the run config rather
    than being inferred here — nlp-service does not know how the audio was
    transcribed and must not guess.
    """
    language = transcript.language or "en"
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            INSERT INTO transcripts
                (consultation_id, run_config_id, uri, asr_backend, asr_model, language)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (consultation_id, run_config_id) DO UPDATE
                SET uri = EXCLUDED.uri,
                    asr_backend = EXCLUDED.asr_backend,
                    asr_model = EXCLUDED.asr_model,
                    language = EXCLUDED.language
            RETURNING id
            """,
            (consultation_id, run_config_id, uri, asr_backend, asr_model, language),
        )
        row = await cur.fetchone()
        assert row is not None  # RETURNING on an upsert always yields a row
        transcript_id = str(row["id"])

        turn_ids: dict[int, str] = {}
        for turn in transcript.turns:
            await cur.execute(
                """
                INSERT INTO turns
                    (transcript_id, turn_index, speaker_label, start_ms, end_ms,
                     text, text_redacted, confidence)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (transcript_id, turn_index) DO UPDATE
                    SET speaker_label = EXCLUDED.speaker_label,
                        start_ms = EXCLUDED.start_ms,
                        end_ms = EXCLUDED.end_ms,
                        text = EXCLUDED.text,
                        text_redacted = EXCLUDED.text_redacted,
                        confidence = EXCLUDED.confidence
                RETURNING id
                """,
                (
                    transcript_id,
                    turn.turn_index,
                    SPEAKER_NAME.get(turn.speaker_label, "unknown"),
                    turn.start_ms,
                    turn.end_ms,
                    turn.text,
                    turn.text_redacted or turn.text,
                    turn.confidence or None,
                ),
            )
            turn_row = await cur.fetchone()
            assert turn_row is not None
            turn_ids[turn.turn_index] = str(turn_row["id"])

    logger.info(
        "transcript materialized",
        extra={
            "extra_fields": {
                "consultation_id": consultation_id,
                "run_config_id": run_config_id,
                "transcript_id": transcript_id,
                "turn_count": len(turn_ids),
            }
        },
    )
    return MaterializedTranscript(transcript_id=transcript_id, turn_id_by_index=turn_ids)


async def persist_graph(
    conn: psycopg.AsyncConnection,
    *,
    consultation_id: str,
    run_config_id: str,
    thoughts: list[thought_pb2.Thought],
    edges: list[thought_pb2.ThoughtEdge],
) -> dict[str, str]:
    """Writes the graph and returns `{build_local_id: database_uuid}` for the
    thoughts.

    Thought ids are `t1`, `t2`, ... within one build (thoughts.py assigns
    them so the edge-prediction call has stable handles); the database
    assigns the durable UUID. The returned map is what lets a caller rewrite
    the in-memory graph's ids to the persisted ones before serializing the
    artifact, so the artifact and the database agree on identity.

    Delete-then-insert rather than upsert: thought ids are positional within
    a build, so a re-run's `t3` is not necessarily the previous run's `t3`,
    and upserting on them would silently blend two builds.
    """
    id_map: dict[str, str] = {}
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        # Edges cascade from thoughts, but deleting them explicitly first
        # keeps the intent readable and does not depend on the FK's ON DELETE.
        await cur.execute(
            "DELETE FROM thought_edges WHERE consultation_id = %s AND run_config_id = %s",
            (consultation_id, run_config_id),
        )
        await cur.execute(
            "DELETE FROM thoughts WHERE consultation_id = %s AND run_config_id = %s",
            (consultation_id, run_config_id),
        )

        for t in thoughts:
            await cur.execute(
                """
                INSERT INTO thoughts
                    (consultation_id, run_config_id, turn_id, speaker, text, entities,
                     category, temporal_anchor, linked_concepts, polarity, confidence,
                     char_start, char_end, turn_index)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    consultation_id,
                    run_config_id,
                    t.turn_id,
                    SPEAKER_NAME.get(t.speaker, "unknown"),
                    t.text,
                    json.dumps([{"text": e.text} for e in t.entities]),
                    CATEGORY_NAME.get(t.category, "other"),
                    t.temporal_anchor or None,
                    json.dumps(list(t.linked_concepts)),
                    POLARITY_NAME.get(t.polarity, "asserted"),
                    t.confidence,
                    t.char_start,
                    t.char_end,
                    t.turn_index,
                ),
            )
            row = await cur.fetchone()
            assert row is not None
            id_map[t.id] = str(row["id"])

        for e in edges:
            src = id_map.get(e.src_thought_id)
            dst = id_map.get(e.dst_thought_id)
            if src is None or dst is None:
                # An edge naming a thought that was dropped before
                # persistence. Skipped rather than failing the whole graph:
                # the edge carries no information without both endpoints.
                logger.warning(
                    "skipping edge with an unpersisted endpoint",
                    extra={
                        "extra_fields": {
                            "consultation_id": consultation_id,
                            "src": e.src_thought_id,
                            "dst": e.dst_thought_id,
                        }
                    },
                )
                continue
            await cur.execute(
                """
                INSERT INTO thought_edges
                    (consultation_id, run_config_id, src_thought_id, dst_thought_id,
                     edge_type, weight, predicted_by, rationale)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    consultation_id,
                    run_config_id,
                    src,
                    dst,
                    EDGE_TYPE_NAME.get(e.edge_type, "temporal"),
                    e.weight,
                    _PREDICTED_BY_NAME.get(e.predicted_by, "rule"),
                    e.rationale or None,
                ),
            )

    logger.info(
        "thought graph persisted",
        extra={
            "extra_fields": {
                "consultation_id": consultation_id,
                "run_config_id": run_config_id,
                "thoughts_written": len(id_map),
                "edges_written": len(edges),
            }
        },
    )
    return id_map


def apply_id_map(
    thoughts: list[thought_pb2.Thought],
    edges: list[thought_pb2.ThoughtEdge],
    id_map: dict[str, str],
) -> tuple[list[thought_pb2.Thought], list[thought_pb2.ThoughtEdge]]:
    """Rewrites build-local ids to persisted UUIDs, in place on copies, so
    the serialized artifact and the database name the same nodes.
    """
    out_thoughts = []
    for t in thoughts:
        thought_copy = thought_pb2.Thought()
        thought_copy.CopyFrom(t)
        thought_copy.id = id_map.get(t.id, t.id)
        out_thoughts.append(thought_copy)
    out_edges = []
    for e in edges:
        if e.src_thought_id not in id_map or e.dst_thought_id not in id_map:
            continue
        edge_copy = thought_pb2.ThoughtEdge()
        edge_copy.CopyFrom(e)
        edge_copy.src_thought_id = id_map[e.src_thought_id]
        edge_copy.dst_thought_id = id_map[e.dst_thought_id]
        out_edges.append(edge_copy)
    return out_thoughts, out_edges


def build_graph(
    *,
    consultation_id: str,
    run_config_id: str,
    thoughts: list[thought_pb2.Thought],
    edges: list[thought_pb2.ThoughtEdge],
    prompt_set_hash: str,
    language: str,
    construction_model: str,
    edge_model: str,
    turn_count: int,
) -> thought_pb2.ThoughtGraph:
    return thought_pb2.ThoughtGraph(
        consultation_id=consultation_id,
        run_config_id=run_config_id,
        thoughts=thoughts,
        edges=edges,
        prompt_set_hash=prompt_set_hash,
        language=language,
        construction_model=construction_model,
        edge_model=edge_model,
        turn_count=turn_count,
    )


def serialize_graph(graph: thought_pb2.ThoughtGraph) -> bytes:
    """protojson with original field names, matching every other artifact
    this system writes (architecture.md §2.2), so one reader handles them
    all.
    """
    return json_format.MessageToJson(graph, preserving_proto_field_name=True, indent=None).encode()


def deserialize_graph(raw: bytes) -> thought_pb2.ThoughtGraph:
    """The other half of "reconstructible for the report figures" — a stored
    artifact must round-trip back into the same message a figure script can
    walk, without a database.
    """
    graph = thought_pb2.ThoughtGraph()
    json_format.Parse(raw, graph, ignore_unknown_fields=True)
    return graph


async def load_graph(
    conn: psycopg.AsyncConnection, *, consultation_id: str, run_config_id: str
) -> thought_pb2.ThoughtGraph:
    """Reconstructs the graph from `thoughts`/`thought_edges` alone.

    This is architecture.md §6's reproducibility guarantee made executable
    for the graph specifically: if this function can rebuild the figure
    source from database rows, the graph is genuinely reproducible from the
    database and not only from a surviving artifact file.
    """
    category_by_name = {v: k for k, v in CATEGORY_NAME.items()}
    polarity_by_name = {v: k for k, v in POLARITY_NAME.items()}
    speaker_by_name = {v: k for k, v in SPEAKER_NAME.items()}
    edge_type_by_name = {v: k for k, v in EDGE_TYPE_NAME.items()}
    predicted_by_name = {v: k for k, v in _PREDICTED_BY_NAME.items()}

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT id, turn_id, turn_index, speaker, text, entities, category,
                   temporal_anchor, linked_concepts, polarity, confidence,
                   char_start, char_end
            FROM thoughts
            WHERE consultation_id = %s AND run_config_id = %s
            ORDER BY turn_index, char_start, id
            """,
            (consultation_id, run_config_id),
        )
        thought_rows = await cur.fetchall()

        await cur.execute(
            """
            SELECT src_thought_id, dst_thought_id, edge_type, weight, predicted_by, rationale
            FROM thought_edges
            WHERE consultation_id = %s AND run_config_id = %s
            ORDER BY src_thought_id, dst_thought_id
            """,
            (consultation_id, run_config_id),
        )
        edge_rows = await cur.fetchall()

    graph = thought_pb2.ThoughtGraph(consultation_id=consultation_id, run_config_id=run_config_id)
    for row in thought_rows:
        entities = row["entities"] or []
        graph.thoughts.append(
            thought_pb2.Thought(
                id=str(row["id"]),
                consultation_id=consultation_id,
                run_config_id=run_config_id,
                turn_id=str(row["turn_id"]),
                turn_index=row["turn_index"] or 0,
                speaker=speaker_by_name.get(
                    row["speaker"], transcript_pb2.SpeakerRole.SPEAKER_ROLE_UNKNOWN
                ),
                text=row["text"],
                entities=[
                    thought_pb2.LinkedEntity(text=str(e.get("text", "")))
                    for e in entities
                    if isinstance(e, dict)
                ],
                category=category_by_name.get(
                    row["category"], thought_pb2.ThoughtCategory.THOUGHT_CATEGORY_OTHER
                ),
                temporal_anchor=row["temporal_anchor"] or "",
                linked_concepts=[str(c) for c in (row["linked_concepts"] or [])],
                polarity=polarity_by_name.get(
                    row["polarity"], thought_pb2.Polarity.POLARITY_ASSERTED
                ),
                confidence=row["confidence"] or 0.0,
                char_start=row["char_start"] or 0,
                char_end=row["char_end"] or 0,
            )
        )
    for row in edge_rows:
        graph.edges.append(
            thought_pb2.ThoughtEdge(
                consultation_id=consultation_id,
                run_config_id=run_config_id,
                src_thought_id=str(row["src_thought_id"]),
                dst_thought_id=str(row["dst_thought_id"]),
                edge_type=edge_type_by_name.get(
                    row["edge_type"], thought_pb2.EdgeType.EDGE_TYPE_TEMPORAL
                ),
                weight=row["weight"],
                predicted_by=predicted_by_name.get(
                    row["predicted_by"], thought_pb2.PredictedBy.PREDICTED_BY_RULE
                ),
                rationale=row["rationale"] or "",
            )
        )
    graph.turn_count = len({t.turn_index for t in graph.thoughts})
    return graph


__all__ = [
    "MaterializedTranscript",
    "materialize_transcript",
    "persist_graph",
    "apply_id_map",
    "build_graph",
    "serialize_graph",
    "deserialize_graph",
    "load_graph",
]
