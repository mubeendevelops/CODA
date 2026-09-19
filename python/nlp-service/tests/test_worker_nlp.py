import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import pytest
from google.protobuf import json_format

from coda.v1 import common_pb2, envelope_pb2, runconfig_pb2, transcript_pb2
from coda_worker_sdk import StageContext
from nlp_service import db
from nlp_service.llm.cassette import CassetteLLMClient, CassetteTurn
from nlp_service.worker import build_nlp_handler

VALID_EXTRACTION = {
    "chief_complaint": {"value": "cough", "source_turn_ids": [0], "confidence": 0.9},
    "hopi": None,
    "past_medical_history": [],
    "medications": [],
    "allergies": [],
    "examination_findings": None,
    "provisional_diagnosis": [],
    "investigations_advised": [],
    "treatment_plan": None,
}


@dataclass
class FakeStorage:
    objects: dict[str, bytes] = field(default_factory=dict)
    env: str = "dev"

    async def get_bytes(self, key: str) -> bytes:
        return self.objects[key]

    async def put_bytes(self, key: str, data: bytes, *, content_type: str = "") -> str:
        self.objects[key] = data
        return "sha256-fake"

    def artifact_key(
        self, consultation_id: str, stage: str, run_config_id: str, kind: str, ext: str
    ) -> str:
        return (
            f"{self.env}/consultations/{consultation_id}/stages/"
            f"{stage}/{run_config_id}/{kind}.{ext}"
        )


class FakeHeartbeat:
    def update(self, *, percent: float, step: str) -> None:
        pass


class FakePool:
    @asynccontextmanager
    async def connection(self) -> AsyncIterator[object]:
        yield object()


def _make_transcript() -> transcript_pb2.Transcript:
    return transcript_pb2.Transcript(
        consultation_id="c1",
        run_config_id="rc1",
        language="en",
        turns=[
            transcript_pb2.Turn(
                turn_index=0,
                speaker_label=transcript_pb2.SpeakerRole.SPEAKER_ROLE_PATIENT,
                text="I have a cough",
                text_redacted="I have a cough",
            ),
            transcript_pb2.Turn(
                turn_index=1,
                speaker_label=transcript_pb2.SpeakerRole.SPEAKER_ROLE_DOCTOR,
                text="How long has this been going on?",
                text_redacted="How long has this been going on?",
            ),
        ],
    )


def _make_envelope(
    *, stage: common_pb2.Stage.ValueType, payload_ref: str
) -> envelope_pb2.StageEnvelope:
    return envelope_pb2.StageEnvelope(
        job_id="j1",
        consultation_id="c1",
        stage=stage,
        attempt=1,
        run_config_id="rc1",
        payload_ref=payload_ref,
    )


@pytest.fixture(autouse=True)
def _no_real_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_hit(conn: object, *, model: str, sha256: str) -> None:
        return None

    async def _no_store(conn: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(db, "get_cached_completion", _no_hit)
    monkeypatch.setattr(db, "store_completion", _no_store)


async def test_handle_nlp_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    written: dict[str, object] = {}

    async def _fetch_run_config(conn: object, run_config_id: str) -> db.RunConfigRow:
        return db.RunConfigRow(
            id=run_config_id,
            arm="baseline",
            config=runconfig_pb2.RunConfig(base_model="qwen/qwen3.8-27b", got_enabled=False),
        )

    async def _write_outputs(conn: object, **kwargs: object) -> None:
        written.update(kwargs)

    monkeypatch.setattr(db, "fetch_run_config", _fetch_run_config)
    monkeypatch.setattr(db, "write_pipeline_outputs", _write_outputs)

    storage = FakeStorage()
    transcript = _make_transcript()
    payload_ref = "dev/consultations/c1/stages/redact/rc1/transcript.json"
    storage.objects[payload_ref] = json_format.MessageToJson(
        transcript, preserving_proto_field_name=True
    ).encode()

    llm_client = CassetteLLMClient(
        turns=[
            CassetteTurn(content=json.dumps(VALID_EXTRACTION), tokens_in=100, tokens_out=50),
            CassetteTurn(content="Patient presented with a cough.", tokens_in=30, tokens_out=20),
        ]
    )

    handler = build_nlp_handler(
        pg_pool=FakePool(),  # type: ignore[arg-type]
        llm_client=llm_client,
        repair_max_attempts=2,
        timeout_s=5.0,
    )

    ctx = StageContext(
        envelope=_make_envelope(stage=common_pb2.Stage.STAGE_NLP, payload_ref=payload_ref),
        storage=storage,  # type: ignore[arg-type]
        redis=None,  # type: ignore[arg-type]
        heartbeat=FakeHeartbeat(),  # type: ignore[arg-type]
        logger=__import__("logging").getLogger("test"),
    )

    output = await handler(ctx)

    assert output.result_ref.endswith("clinical_note.json")
    assert output.metrics.schema_valid is True
    assert output.metrics.repair_attempts == 0
    assert output.metrics.tokens_in == 130
    assert output.metrics.tokens_out == 70
    assert output.metrics.llm_calls == 2
    assert output.metrics.model_ids == ["qwen/qwen3.8-27b"]

    assert written["consultation_id"] == "c1"
    assert written["run_config_id"] == "rc1"
    assert written["summary_text"] == "Patient presented with a cough."

    note_bytes = storage.objects[output.result_ref]
    note_json = json.loads(note_bytes)
    assert note_json["chief_complaint"]["value"] == "cough"

    summary_key = "dev/consultations/c1/stages/nlp/rc1/summary.json"
    summary_json = json.loads(storage.objects[summary_key])
    assert summary_json["text"] == "Patient presented with a cough."


async def test_handle_nlp_got_arm_no_longer_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `got_enabled=true` config used to be rejected outright with
    `GOT_NOT_IMPLEMENTED` before reading anything. It now runs GoT-HCS
    Modules 1-2 (thought construction and graph assembly) for real and fails
    only at the unbuilt reasoning half — see
    `tests/graph/test_worker_got_arm.py` for the full GoT-arm behaviour.

    What this test still guards is the ordering: the handler must reach the
    transcript before deciding anything about the GoT arm. The
    missing-artifact KeyError below is the evidence — under the old code the
    stage raised before `get_bytes` was ever called.
    """

    async def _fetch_run_config(conn: object, run_config_id: str) -> db.RunConfigRow:
        return db.RunConfigRow(
            id=run_config_id,
            arm="got_k2",
            config=runconfig_pb2.RunConfig(base_model="qwen/qwen3.8-27b", got_enabled=True),
        )

    monkeypatch.setattr(db, "fetch_run_config", _fetch_run_config)

    handler = build_nlp_handler(
        pg_pool=FakePool(),  # type: ignore[arg-type]
        llm_client=CassetteLLMClient(turns=[]),
        repair_max_attempts=2,
        timeout_s=5.0,
    )
    ctx = StageContext(
        envelope=_make_envelope(stage=common_pb2.Stage.STAGE_NLP, payload_ref="dev/x/y.json"),
        storage=FakeStorage(),  # type: ignore[arg-type]
        redis=None,  # type: ignore[arg-type]
        heartbeat=FakeHeartbeat(),  # type: ignore[arg-type]
        logger=__import__("logging").getLogger("test"),
    )

    with pytest.raises(KeyError):
        await handler(ctx)
