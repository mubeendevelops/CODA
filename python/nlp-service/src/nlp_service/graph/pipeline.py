"""Module 1 + Module 2 end to end: transcript in, persisted thought graph and
serialized artifact out.

This is the seam the rest of Phase 6 attaches to. Candidate generation,
rubric scoring, K-iteration refinement and distillation (Modules 5 and 6) are
not built here; what they need from this stage is exactly `GraphBuildResult`
— a graph, plus `retrieve_field_context` to ask it for a field's
neighbourhood.

Model roles follow claude_context.md §4's assignment table:
construction on `base_model` (the same model the baseline arm reads the
transcript with, so the ablation isolates graph reasoning rather than
confounding it with a model swap), edge prediction on `structural_model`
(also a separate Groq quota bucket — §8's mitigation #1).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import psycopg

from coda.v1 import thought_pb2, transcript_pb2
from coda_worker_sdk.errors import FatalError
from nlp_service import prompts
from nlp_service.graph import store
from nlp_service.graph.edges import assemble_edges
from nlp_service.graph.thoughts import construct_thoughts

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GraphBuildResult:
    graph: thought_pb2.ThoughtGraph
    """Ids are the persisted database UUIDs, not build-local `t{n}` handles,
    so the artifact and the database name the same nodes."""
    artifact_key: str
    tokens_in: int
    tokens_out: int
    llm_calls: int
    cache_hits: int
    repair_attempts: int
    rule_edge_count: int
    llm_edge_count: int
    dropped_thoughts: list[str]
    wall_ms: int


async def build_thought_graph(
    *,
    conn: psycopg.AsyncConnection,
    storage: object,
    llm_client: object,
    transcript: transcript_pb2.Transcript,
    consultation_id: str,
    run_config_id: str,
    transcript_uri: str,
    base_model: str,
    structural_model: str,
    asr_backend: str,
    asr_model: str,
    repair_max_attempts: int,
    timeout_s: float,
    language: str = prompts.DEFAULT_LANGUAGE,
) -> GraphBuildResult:
    """`storage` is a `coda_worker_sdk` storage client (`artifact_key`,
    `put_bytes`); `llm_client` an `LLMClient`. Both are passed rather than
    constructed here so tests substitute fakes without patching.
    """
    started = time.monotonic()
    if not transcript.turns:
        raise FatalError(
            "cannot build a thought graph from a transcript with no turns",
            code="EMPTY_TRANSCRIPT",
        )

    # 0. Materialize transcripts/turns so thoughts.turn_id has real rows to
    #    reference. Nothing else in the system writes these tables.
    materialized = await store.materialize_transcript(
        conn,
        transcript=transcript,
        consultation_id=consultation_id,
        run_config_id=run_config_id,
        uri=transcript_uri,
        asr_backend=asr_backend,
        asr_model=asr_model,
    )

    # 1. Module 1 — thought construction, on base_model.
    construction = await construct_thoughts(
        conn=conn,
        llm_client=llm_client,  # type: ignore[arg-type]
        model=base_model,
        transcript=transcript,
        consultation_id=consultation_id,
        run_config_id=run_config_id,
        turn_id_by_index=materialized.turn_id_by_index,
        repair_max_attempts=repair_max_attempts,
        timeout_s=timeout_s,
        language=language,
    )
    if not construction.thoughts:
        raise FatalError(
            "thought construction produced no thoughts for a non-empty transcript; "
            "a graph cannot be built and the GoT arm has nothing to reason over",
            code="NO_THOUGHTS_CONSTRUCTED",
        )

    # 2. Module 2 — graph assembly: rule temporal edges + LLM-predicted
    #    edges, on structural_model.
    assembly = await assemble_edges(
        conn=conn,
        llm_client=llm_client,  # type: ignore[arg-type]
        model=structural_model,
        thoughts=construction.thoughts,
        consultation_id=consultation_id,
        run_config_id=run_config_id,
        repair_max_attempts=repair_max_attempts,
        timeout_s=timeout_s,
        language=language,
    )

    # 3. Persist, then rewrite build-local ids to the persisted UUIDs so the
    #    artifact and the database agree on node identity.
    id_map = await store.persist_graph(
        conn,
        consultation_id=consultation_id,
        run_config_id=run_config_id,
        thoughts=construction.thoughts,
        edges=assembly.edges,
    )
    thoughts, edges = store.apply_id_map(construction.thoughts, assembly.edges, id_map)

    graph = store.build_graph(
        consultation_id=consultation_id,
        run_config_id=run_config_id,
        thoughts=thoughts,
        edges=edges,
        prompt_set_hash=prompts.prompt_set_hash(language=language),
        language=language,
        construction_model=base_model,
        edge_model=structural_model,
        turn_count=len(transcript.turns),
    )

    # 4. Serialize the artifact. §3.3 artifact key, `thought_graph` kind.
    artifact_key = storage.artifact_key(  # type: ignore[attr-defined]
        consultation_id, "nlp", run_config_id, "thought_graph", "json"
    )
    await storage.put_bytes(  # type: ignore[attr-defined]
        artifact_key, store.serialize_graph(graph), content_type="application/json"
    )

    wall_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "thought graph build complete",
        extra={
            "extra_fields": {
                "consultation_id": consultation_id,
                "run_config_id": run_config_id,
                "thoughts": len(graph.thoughts),
                "edges": len(graph.edges),
                "rule_edges": assembly.rule_edge_count,
                "llm_edges": assembly.llm_edge_count,
                "artifact_key": artifact_key,
                "wall_ms": wall_ms,
            }
        },
    )
    return GraphBuildResult(
        graph=graph,
        artifact_key=artifact_key,
        tokens_in=construction.tokens_in + assembly.tokens_in,
        tokens_out=construction.tokens_out + assembly.tokens_out,
        llm_calls=construction.llm_calls + assembly.llm_calls,
        cache_hits=construction.cache_hits + assembly.cache_hits,
        repair_attempts=construction.repair_attempts + assembly.repair_attempts,
        rule_edge_count=assembly.rule_edge_count,
        llm_edge_count=assembly.llm_edge_count,
        dropped_thoughts=construction.dropped,
        wall_ms=wall_ms,
    )


__all__ = ["GraphBuildResult", "build_thought_graph"]
