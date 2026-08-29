"""protojson encode/decode against the generated coda.v1 contract classes.

Mirrors go/internal/queue/codec.go field-for-field: the same denormalised
scalars (job_id, stage, attempt, idempotency_key, trace_id) sit alongside the
protojson payload so a stream entry stays greppable via `XRANGE`, and
schema_version is checked on every decode (architecture.md §2.2/§3.1).
"""

from __future__ import annotations

from typing import TypeVar

from google.protobuf import json_format
from google.protobuf.message import Message

from coda.v1 import common_pb2, envelope_pb2
from coda_worker_sdk.streams import (
    FIELD_ATTEMPT,
    FIELD_IDEMPOTENCY_KEY,
    FIELD_JOB_ID,
    FIELD_KIND,
    FIELD_PAYLOAD,
    FIELD_SCHEMA_VERSION,
    FIELD_STAGE,
    FIELD_TRACE_ID,
    KIND_DEAD_LETTER,
    KIND_STAGE_ENVELOPE,
    KIND_STAGE_HEARTBEAT,
    KIND_STAGE_RESULT,
    SCHEMA_VERSION,
    stage_name,
)

M = TypeVar("M", bound=Message)


class SchemaUnsupportedError(Exception):
    """A message declared a schema_version this build does not implement
    (architecture.md §2.2). The caller must classify this FATAL, never retry
    it — retrying cannot make an unimplemented contract implementable."""


def encode_fields(kind: str, message: Message, extra: dict[str, str]) -> dict[str, str]:
    """Render a proto message into the flat Redis stream entry shape used by
    every stream in this system. `extra` supplies the denormalised scalars;
    empty values are omitted, matching the Go side's `fields()`.
    """
    payload = json_format.MessageToJson(message, preserving_proto_field_name=True, indent=None)
    out: dict[str, str] = {
        FIELD_KIND: kind,
        FIELD_PAYLOAD: payload,
        FIELD_SCHEMA_VERSION: str(SCHEMA_VERSION),
    }
    for k, v in extra.items():
        if v:
            out[k] = v
    return out


def result_fields(res: envelope_pb2.StageResult) -> dict[str, str]:
    return encode_fields(
        KIND_STAGE_RESULT,
        res,
        {
            FIELD_JOB_ID: res.job_id,
            FIELD_STAGE: stage_name(res.stage),
            FIELD_ATTEMPT: str(res.attempt),
            FIELD_IDEMPOTENCY_KEY: res.idempotency_key,
            FIELD_TRACE_ID: res.trace_id,
        },
    )


def heartbeat_fields(hb: envelope_pb2.StageHeartbeat) -> dict[str, str]:
    return encode_fields(
        KIND_STAGE_HEARTBEAT,
        hb,
        {
            FIELD_JOB_ID: hb.job_id,
            FIELD_STAGE: stage_name(hb.stage),
            FIELD_ATTEMPT: str(hb.attempt),
            FIELD_TRACE_ID: hb.trace_id,
        },
    )


def decode(fields: dict[str, str], want_kind: str, message: M) -> M:
    """Unmarshal a message payload after checking its kind and schema
    version. Mirrors go/internal/queue/codec.go's `decode`.
    """
    got_kind = fields.get(FIELD_KIND, "")
    if got_kind and got_kind != want_kind:
        raise ValueError(f"coda_worker_sdk: message is a {got_kind}, want {want_kind}")

    raw_version = fields.get(FIELD_SCHEMA_VERSION, "")
    if raw_version:
        try:
            version = int(raw_version)
        except ValueError as exc:
            raise ValueError(
                f"coda_worker_sdk: unparseable schema_version {raw_version!r}"
            ) from exc
        if version != SCHEMA_VERSION:
            raise SchemaUnsupportedError(
                f"message declares schema_version {version}, this build implements {SCHEMA_VERSION}"
            )

    payload = fields.get(FIELD_PAYLOAD, "")
    if not payload:
        raise ValueError(f"coda_worker_sdk: message has no {FIELD_PAYLOAD!r} field")
    json_format.Parse(payload, message, ignore_unknown_fields=True)
    return message


def decode_envelope(fields: dict[str, str]) -> envelope_pb2.StageEnvelope:
    return decode(fields, KIND_STAGE_ENVELOPE, envelope_pb2.StageEnvelope())


def decode_dead_letter(fields: dict[str, str]) -> envelope_pb2.DeadLetter:
    return decode(fields, KIND_DEAD_LETTER, envelope_pb2.DeadLetter())


__all__ = [
    "SchemaUnsupportedError",
    "encode_fields",
    "result_fields",
    "heartbeat_fields",
    "decode",
    "decode_envelope",
    "decode_dead_letter",
    "common_pb2",
]
