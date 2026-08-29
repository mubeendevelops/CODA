import pytest

from coda.v1 import common_pb2, envelope_pb2
from coda_worker_sdk.codec import (
    SchemaUnsupportedError,
    decode_envelope,
    encode_fields,
    result_fields,
)
from coda_worker_sdk.streams import KIND_STAGE_ENVELOPE, SCHEMA_VERSION


def test_envelope_round_trips_through_encode_decode() -> None:
    env = envelope_pb2.StageEnvelope(
        job_id="job-1",
        consultation_id="c-1",
        stage=common_pb2.Stage.STAGE_ASR,
        attempt=1,
        idempotency_key="idem-1",
        trace_id="trace-1",
        schema_version=SCHEMA_VERSION,
    )
    fields = encode_fields(
        KIND_STAGE_ENVELOPE,
        env,
        {"job_id": env.job_id, "stage": "asr", "attempt": "1"},
    )
    assert fields["kind"] == KIND_STAGE_ENVELOPE
    assert fields["job_id"] == "job-1"
    assert fields["schema_version"] == str(SCHEMA_VERSION)

    decoded = decode_envelope(fields)
    assert decoded.job_id == "job-1"
    assert decoded.stage == common_pb2.Stage.STAGE_ASR
    assert decoded.idempotency_key == "idem-1"


def test_decode_rejects_unsupported_schema_version() -> None:
    env = envelope_pb2.StageEnvelope(job_id="job-1")
    fields = encode_fields(KIND_STAGE_ENVELOPE, env, {"job_id": "job-1"})
    fields["schema_version"] = str(SCHEMA_VERSION + 1)
    with pytest.raises(SchemaUnsupportedError):
        decode_envelope(fields)


def test_result_fields_omit_empty_extras() -> None:
    res = envelope_pb2.StageResult(job_id="job-1", status=common_pb2.Status.STATUS_OK)
    fields = result_fields(res)
    assert "trace_id" not in fields  # empty trace_id was never set
    assert fields["job_id"] == "job-1"
