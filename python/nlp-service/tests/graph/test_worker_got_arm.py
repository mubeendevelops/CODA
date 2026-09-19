"""The STAGE_NLP handler on a GoT-arm run config, from the Modules 1-2 side.

What this pins is as much a policy as a behaviour: the GoT arm builds and
persists a real thought graph, hands *that graph* to the reasoning half, and
if reasoning fails it **fails loudly** rather than falling through to
single-pass extraction. Falling through would put an unlabeled control result
into the ablation table under a GoT run_config, which is the one failure mode
that would silently corrupt the research result this whole pipeline exists to
produce.

Reasoning (Modules 5-6) is stubbed here on purpose. These tests exist to pin
the graph half and the seam between the halves; driving real candidate
generation, scoring and refinement through a cassette would make every
assertion below depend on prompt text that has nothing to do with the graph.
Modules 5-6 have their own coverage under `tests/reasoning/`.

Superseded decision #93: this file previously asserted the GoT arm raised
`GOT_REASONING_NOT_IMPLEMENTED` after persisting its graph, which was correct
while Modules 5-6 did not exist. They do now, so the loud-failure guarantee is
re-pinned where it still bites — a reasoning error must propagate, not degrade
to a baseline note.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from fixtures import transcripts
from google.protobuf import json_format

from coda.v1 import clinical_pb2, common_pb2, envelope_pb2, runconfig_pb2, thought_pb2
from coda_worker_sdk.errors import FatalError
from nlp_service import db
from nlp_service.graph import store
from nlp_service.llm.cassette import CassetteLLMClient, CassetteTurn
from nlp_service.reasoning.pipeline import ReasoningResult
from nlp_service.worker import build_nlp_handler

CASSETTES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "cassettes"

Pol = thought_pb2.Polarity
EdgeT = thought_pb2.EdgeType


class FakeHeartbeat:
    def __init__(self) -> None:
        self.steps: list[str] = []

    def update(self, *, percent: float, step: str) -> None:
        self.steps.append(step)


class FakeCtx:
    def __init__(self, envelope, storage, logger) -> None:  # type: ignore[no-untyped-def]
        self.envelope = envelope
        self.storage = storage
        self.logger = logger
        self.heartbeat = FakeHeartbeat()


@pytest.fixture
def got_run_config() -> db.RunConfigRow:
    return db.RunConfigRow(
        id="rc1",
        arm="got_k2",
        config=runconfig_pb2.RunConfig(
            arm="got_k2",
            base_model="qwen/qwen3.8-27b",
            structural_model="qwen/qwen3.6-27b",
            asr_backend="faster_whisper_local",
            asr_model="medium",
            got_enabled=True,
            graph_context_enabled=True,
            n_candidates=3,
            k_iterations=2,
        ),
    )


async def run_got_stage(monkeypatch, fake_storage, got_run_config, fake_conn, reasoning=None):  # type: ignore[no-untyped-def]
    """Drives STAGE_NLP on a GoT run config with Modules 5-6 stubbed.

    `reasoning` is an async `(**kwargs) -> ReasoningResult` replacing
    `run_reasoning`; the default records what the graph half handed it and
    returns an otherwise-empty successful result.
    """
    import logging

    from nlp_service import worker as worker_mod

    transcript = transcripts.negation_transcript()
    payload_ref = "dev/consultations/c1/stages/redact/rc1/transcript.json"
    fake_storage.objects[payload_ref] = json_format.MessageToJson(
        transcript, preserving_proto_field_name=True
    ).encode()

    async def _fetch_run_config(conn: object, run_config_id: str) -> db.RunConfigRow:
        return got_run_config

    materialized: dict[str, object] = {}

    async def _materialize(conn, **kwargs):  # type: ignore[no-untyped-def]
        materialized.update(kwargs)
        return store.MaterializedTranscript(
            transcript_id="tr1",
            turn_id_by_index={t.turn_index: f"uuid-turn-{t.turn_index}" for t in transcript.turns},
        )

    persisted: dict[str, object] = {}

    async def _persist(conn, *, consultation_id, run_config_id, thoughts, edges):  # type: ignore[no-untyped-def]
        persisted["thoughts"] = list(thoughts)
        persisted["edges"] = list(edges)
        return {t.id: f"uuid-{t.id}" for t in thoughts}

    seen: dict[str, object] = {}

    async def _default_reasoning(**kwargs):  # type: ignore[no-untyped-def]
        # Snapshot object storage as reasoning sees it, so a test can assert
        # the graph artifact was already durable before this point.
        seen["graph"] = kwargs["graph"]
        seen["storage_keys"] = set(fake_storage.objects)
        return ReasoningResult(
            note=clinical_pb2.ClinicalNote(),
            summary_text="stub summary",
            outcomes={},
            trace_uris={},
            note_artifact_key="dev/consultations/c1/stages/nlp/rc1/clinical_note.json",
            summary_artifact_key="dev/consultations/c1/stages/nlp/rc1/summary.json",
            tokens_in=0,
            tokens_out=0,
            llm_calls=0,
            cache_hits=0,
            repair_attempts=0,
            wall_ms=0,
        )

    monkeypatch.setattr(db, "fetch_run_config", _fetch_run_config)
    monkeypatch.setattr(store, "materialize_transcript", _materialize)
    monkeypatch.setattr(store, "persist_graph", _persist)
    monkeypatch.setattr(worker_mod, "run_reasoning", reasoning or _default_reasoning)

    data = json.loads((CASSETTES / "negation.json").read_text())
    client = CassetteLLMClient(turns=[CassetteTurn(**t) for t in data])

    from conftest import FakePool

    handler = build_nlp_handler(
        pg_pool=FakePool(),  # type: ignore[arg-type]
        llm_client=client,
        repair_max_attempts=2,
        timeout_s=5.0,
    )
    ctx = FakeCtx(
        envelope_pb2.StageEnvelope(
            job_id="j1",
            consultation_id="c1",
            stage=common_pb2.Stage.STAGE_NLP,
            attempt=1,
            run_config_id="rc1",
            payload_ref=payload_ref,
        ),
        fake_storage,
        logging.getLogger("test"),
    )
    return handler, ctx, materialized, persisted, client, seen


async def test_got_arm_persists_a_real_graph_and_hands_it_to_reasoning(
    monkeypatch: pytest.MonkeyPatch, fake_storage, got_run_config, fake_conn
) -> None:
    """Modules 1-2 really run, are durable, and are what Modules 5-6 receive.

    The negation fixture is the one that makes this worth asserting: if the
    graph reaching reasoning had lost either the negated thought or the edge
    binding it to what it negates, generation would be handed an asserted
    symptom with no trace of its retraction.
    """
    handler, ctx, materialized, persisted, _, seen = await run_got_stage(
        monkeypatch, fake_storage, got_run_config, fake_conn
    )

    output = await handler(ctx)  # type: ignore[arg-type]
    assert output.result_ref.endswith("clinical_note.json")

    # The graph work really happened and is durable.
    assert persisted["thoughts"]
    assert persisted["edges"]
    assert any(t.polarity == Pol.POLARITY_NEGATED for t in persisted["thoughts"])  # type: ignore[union-attr]
    assert any(e.edge_type == EdgeT.EDGE_TYPE_NEGATION for e in persisted["edges"])  # type: ignore[union-attr]

    # ...and the same graph, rewritten to its persisted ids, is what
    # reasoning got. `persist_graph` returns the build-local -> database id
    # map (here `t1` -> `uuid-t1`); reasoning must see the durable ids, so a
    # trace can be joined back to a `thoughts` row.
    graph = seen["graph"]
    assert [t.id for t in graph.thoughts] == [
        f"uuid-{t.id}"
        for t in persisted["thoughts"]  # type: ignore[union-attr]
    ]
    assert any(e.edge_type == EdgeT.EDGE_TYPE_NEGATION for e in graph.edges)  # type: ignore[union-attr]
    assert all(t.id.startswith("uuid-") for t in graph.thoughts)  # type: ignore[union-attr]


async def test_got_arm_does_not_fall_back_to_single_pass_when_reasoning_fails(
    monkeypatch: pytest.MonkeyPatch, fake_storage, got_run_config, fake_conn
) -> None:
    """The policy that outlived decision #93.

    A GoT run whose reasoning half fails must fail the stage. Degrading to
    single-pass extraction would write a control result into the ablation
    table under a GoT run_config — the one failure mode that silently
    corrupts the research result rather than losing it.
    """

    async def _boom(**kwargs):  # type: ignore[no-untyped-def]
        raise FatalError("judge returned unusable output", code="GOT_REASONING_FAILED")

    handler, ctx, _, persisted, _, _ = await run_got_stage(
        monkeypatch, fake_storage, got_run_config, fake_conn, reasoning=_boom
    )

    def _explode(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("a failed GoT run must never fall back to single-pass extraction")

    monkeypatch.setattr("nlp_service.worker.run_extraction", _explode)

    with pytest.raises(FatalError) as exc:
        await handler(ctx)  # type: ignore[arg-type]
    assert exc.value.code == "GOT_REASONING_FAILED"

    # The graph is still durable — a failed reasoning run leaves a case-study
    # artifact behind rather than nothing.
    assert persisted["thoughts"]
    assert not [k for k in fake_storage.objects if k.endswith("clinical_note.json")]
    assert [k for k in fake_storage.objects if k.endswith("thought_graph.json")]


async def test_got_arm_writes_the_thought_graph_artifact_before_reasoning(
    monkeypatch: pytest.MonkeyPatch, fake_storage, got_run_config, fake_conn
) -> None:
    """The artifact must be in object storage before reasoning is entered, so
    a case-study figure can be drawn from a run that never produced a note."""
    handler, ctx, _, _, _, seen = await run_got_stage(
        monkeypatch, fake_storage, got_run_config, fake_conn
    )
    await handler(ctx)  # type: ignore[arg-type]

    keys = [k for k in fake_storage.objects if k.endswith("thought_graph.json")]
    assert len(keys) == 1
    # §3.3 artifact key shape: partitioned by consultation, stage, run config.
    assert keys[0] == "dev/consultations/c1/stages/nlp/rc1/thought_graph.json"
    # Durable *before* reasoning ran, not merely by the end of the stage.
    assert keys[0] in seen["storage_keys"]  # type: ignore[operator]

    graph = store.deserialize_graph(fake_storage.objects[keys[0]])
    assert graph.consultation_id == "c1"
    assert graph.run_config_id == "rc1"
    assert graph.construction_model == "qwen/qwen3.8-27b"
    assert graph.edge_model == "qwen/qwen3.6-27b"
    assert graph.prompt_set_hash
    assert graph.turn_count == 11
    # Ids in the artifact are the persisted ones, not build-local handles.
    assert all(t.id.startswith("uuid-") for t in graph.thoughts)


async def test_got_arm_uses_base_model_for_thoughts_and_structural_for_edges(
    monkeypatch: pytest.MonkeyPatch, fake_storage, got_run_config, fake_conn
) -> None:
    """claude_context.md §4's model assignment, and §8's quota-bucket split.
    Construction shares the baseline arm's model so the ablation isolates
    graph reasoning; edge typing runs on the structural model's separate
    bucket.
    """
    handler, ctx, _, _, client, _ = await run_got_stage(
        monkeypatch, fake_storage, got_run_config, fake_conn
    )
    await handler(ctx)  # type: ignore[arg-type]

    # Exactly two calls: reasoning is stubbed, so these are the graph half's.
    assert len(client.calls) == 2
    assert client.calls[0]["model"] == "qwen/qwen3.8-27b"
    assert client.calls[1]["model"] == "qwen/qwen3.6-27b"
    assert client.calls[0]["model"] != client.calls[1]["model"]


async def test_baseline_arm_is_untouched_by_the_got_path(
    monkeypatch: pytest.MonkeyPatch, fake_storage, fake_conn
) -> None:
    """Phase 4's frozen baseline must not change behaviour: a
    `got_enabled=false` config still goes to single-pass extraction and never
    builds a graph."""
    import logging

    transcript = transcripts.negation_transcript()
    payload_ref = "dev/consultations/c1/stages/redact/rc1/transcript.json"
    fake_storage.objects[payload_ref] = json_format.MessageToJson(
        transcript, preserving_proto_field_name=True
    ).encode()

    async def _fetch_run_config(conn: object, run_config_id: str) -> db.RunConfigRow:
        return db.RunConfigRow(
            id="rc1",
            arm="baseline",
            config=runconfig_pb2.RunConfig(base_model="qwen/qwen3.8-27b", got_enabled=False),
        )

    written: dict[str, object] = {}

    async def _write_outputs(conn: object, **kwargs: object) -> None:
        written.update(kwargs)

    def _explode(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("the baseline arm must never build a thought graph")

    monkeypatch.setattr(db, "fetch_run_config", _fetch_run_config)
    monkeypatch.setattr(db, "write_pipeline_outputs", _write_outputs)
    monkeypatch.setattr(store, "materialize_transcript", _explode)

    valid_note = {
        "chief_complaint": {"value": "sore throat", "source_turn_ids": [1], "confidence": 0.9},
        "hopi": None,
        "past_medical_history": [],
        "medications": [],
        "allergies": [],
        "examination_findings": None,
        "provisional_diagnosis": [],
        "investigations_advised": [],
        "treatment_plan": None,
    }
    client = CassetteLLMClient(
        turns=[
            CassetteTurn(content=json.dumps(valid_note)),
            CassetteTurn(
                content="A consultation about a sore throat with a documented "
                "correction to the patient's reported allergy."
            ),
        ]
    )

    from conftest import FakePool

    handler = build_nlp_handler(
        pg_pool=FakePool(),  # type: ignore[arg-type]
        llm_client=client,
        repair_max_attempts=2,
        timeout_s=5.0,
    )
    ctx = FakeCtx(
        envelope_pb2.StageEnvelope(
            job_id="j1",
            consultation_id="c1",
            stage=common_pb2.Stage.STAGE_NLP,
            attempt=1,
            run_config_id="rc1",
            payload_ref=payload_ref,
        ),
        fake_storage,
        logging.getLogger("test"),
    )
    output = await handler(ctx)  # type: ignore[arg-type]

    assert output.result_ref.endswith("clinical_note.json")
    assert written["summary_text"]
    assert not [k for k in fake_storage.objects if "thought_graph" in k]
