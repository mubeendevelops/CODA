"""Builders for the protos these tests need. Keeps the assertions about
behaviour instead of about proto construction."""

from __future__ import annotations

from coda.v1 import got_pb2, runconfig_pb2, thought_pb2

Cat = thought_pb2.ThoughtCategory
Pol = thought_pb2.Polarity
EdgeT = thought_pb2.EdgeType


def thought(
    tid: str,
    turn: int,
    text: str,
    entities: list[str],
    *,
    category: int = Cat.THOUGHT_CATEGORY_SYMPTOM,
    polarity: int = Pol.POLARITY_ASSERTED,
    char_start: int = 0,
) -> thought_pb2.Thought:
    return thought_pb2.Thought(
        id=tid,
        consultation_id="c1",
        run_config_id="rc1",
        turn_id=f"uuid-turn-{turn}",
        turn_index=turn,
        text=text,
        entities=[thought_pb2.LinkedEntity(text=e) for e in entities],
        category=category,
        polarity=polarity,
        confidence=0.9,
        char_start=char_start,
        char_end=char_start + max(1, len(text)),
    )


def edge(src: str, dst: str, edge_type: int, weight: float = 0.8) -> thought_pb2.ThoughtEdge:
    return thought_pb2.ThoughtEdge(
        consultation_id="c1",
        run_config_id="rc1",
        src_thought_id=src,
        dst_thought_id=dst,
        edge_type=edge_type,
        weight=weight,
        predicted_by=thought_pb2.PredictedBy.PREDICTED_BY_LLM,
    )


def candidate(
    text: str,
    *,
    index: int = 0,
    items: list[str] | None = None,
    source_thought_ids: list[str] | None = None,
    variant: str = "conservative",
) -> got_pb2.Candidate:
    return got_pb2.Candidate(
        index=index,
        text=text,
        items=items or [],
        source_thought_ids=source_thought_ids or [],
        generated_by_model="qwen/qwen3.8-27b",
        variant=variant,
    )


def run_config(**kwargs: object) -> runconfig_pb2.RunConfig:
    defaults: dict[str, object] = {
        "arm": "got_k2",
        "base_model": "qwen/qwen3.8-27b",
        "structural_model": "qwen/qwen3.6-27b",
        "judge_model": "openai/gpt-oss-20b",
        "asr_backend": "faster_whisper_local",
        "asr_model": "medium",
        "got_enabled": True,
        "graph_context_enabled": True,
        "n_candidates": 3,
        "k_iterations": 2,
        "temperature": 0.0,
    }
    defaults.update(kwargs)
    return runconfig_pb2.RunConfig(**defaults)  # type: ignore[arg-type]


__all__ = ["thought", "edge", "candidate", "run_config", "Cat", "Pol", "EdgeT"]
