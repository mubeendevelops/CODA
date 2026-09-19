"""Single-pass extraction: one prompted LLM call producing the 8 clinical
fields as structured JSON, with a bounded repair loop on validation failure
(Phase 4 requirement — claude_context.md §7 decision #66). JSON validity is
recorded as a metric (`schema_valid`/`repair_attempts`, common.proto), never
silently patched: if the repair budget is exhausted without a valid
response, this raises `FatalError` rather than guessing a value into place.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import psycopg
from google.protobuf import json_format

from coda.v1 import clinical_pb2
from coda_worker_sdk.errors import FatalError
from nlp_service import prompts
from nlp_service.llm.client import LLMClient
from nlp_service.llm_cache import complete_cached
from nlp_service.schema import validate_extraction

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    note: clinical_pb2.ClinicalNote
    tokens_in: int
    tokens_out: int
    llm_calls: int
    cache_hits: int
    schema_valid: bool
    repair_attempts: int


async def run_extraction(
    *,
    conn: psycopg.AsyncConnection,
    llm_client: LLMClient,
    model: str,
    transcript_turns_text: str,
    known_turn_ids: set[int],
    consultation_id: str,
    run_config_id: str,
    repair_max_attempts: int,
    timeout_s: float,
    language: str = prompts.DEFAULT_LANGUAGE,
) -> ExtractionResult:
    pr = prompts.load_extraction_prompts(language=language)

    tokens_in = 0
    tokens_out = 0
    cache_hits = 0

    system_prompt = pr.system
    user_prompt = pr.user_template.format(transcript_turns=transcript_turns_text)
    last_error = ""
    last_content = ""

    repair_attempts = 0
    for attempt in range(repair_max_attempts + 1):
        completion, cache_hit = await complete_cached(
            conn,
            llm_client,
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.0,
            timeout_s=timeout_s,
            json_mode=True,
        )
        tokens_in += completion.tokens_in
        tokens_out += completion.tokens_out
        if cache_hit:
            cache_hits += 1

        result = validate_extraction(completion.content, known_turn_ids=known_turn_ids)
        if result.valid:
            assert result.parsed is not None
            note = _to_clinical_note(
                result.parsed, consultation_id=consultation_id, run_config_id=run_config_id
            )
            return ExtractionResult(
                note=note,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                llm_calls=attempt + 1,
                cache_hits=cache_hits,
                schema_valid=True,
                repair_attempts=repair_attempts,
            )

        last_error = result.error
        last_content = completion.content
        logger.warning(
            "extraction validation failed",
            extra={
                "extra_fields": {
                    "consultation_id": consultation_id,
                    "attempt": attempt,
                    "error": last_error,
                }
            },
        )

        if attempt >= repair_max_attempts:
            break

        repair_attempts += 1
        user_prompt = (
            pr.user_template.format(transcript_turns=transcript_turns_text)
            + "\n\n"
            + pr.repair_addendum_template.format(
                validation_error=last_error, previous_response=last_content
            )
        )

    # NOTE: coda_worker_sdk.worker.StageWorker's exception boundary discards
    # any StageMetrics on a raised WorkerError (StageResult.metrics stays a
    # zero-valued default on a FATAL/RETRYABLE outcome — see worker.py's
    # `_handle_envelope`, which only reads `output.metrics` on the success
    # path). schema_valid=false/repair_attempts are therefore NOT visible in
    # the persisted StageResult for this failure path; logging them here
    # structurally is the only record until that SDK gap is closed.
    logger.error(
        "extraction exhausted repair budget without a schema-valid response",
        extra={
            "extra_fields": {
                "consultation_id": consultation_id,
                "run_config_id": run_config_id,
                "schema_valid": False,
                "repair_attempts": repair_attempts,
                "last_error": last_error,
            }
        },
    )
    raise FatalError(
        f"extraction did not produce schema-valid JSON after {repair_attempts} repair "
        f"attempt(s); last error: {last_error}",
        code="EXTRACTION_SCHEMA_INVALID",
    )


def _to_clinical_note(
    data: dict[str, object], *, consultation_id: str, run_config_id: str
) -> clinical_pb2.ClinicalNote:
    def fv(d: dict[str, object] | None) -> clinical_pb2.FieldValue | None:
        if d is None:
            return None
        fv_msg = clinical_pb2.FieldValue()
        json_format.ParseDict(d, fv_msg, ignore_unknown_fields=True)
        return fv_msg

    def fv_list(items: list[object]) -> list[clinical_pb2.FieldValue]:
        out = []
        for item in items:
            assert isinstance(item, dict)
            parsed = fv(item)
            assert parsed is not None
            out.append(parsed)
        return out

    note = clinical_pb2.ClinicalNote(
        consultation_id=consultation_id,
        run_config_id=run_config_id,
        version=1,
        status=clinical_pb2.NoteStatus.NOTE_STATUS_DRAFT,
        past_medical_history=fv_list(data.get("past_medical_history") or []),  # type: ignore[arg-type]
        provisional_diagnosis=fv_list(data.get("provisional_diagnosis") or []),  # type: ignore[arg-type]
        investigations_advised=fv_list(data.get("investigations_advised") or []),  # type: ignore[arg-type]
        medications_allergies=clinical_pb2.MedicationsAllergies(
            medications=fv_list(data.get("medications") or []),  # type: ignore[arg-type]
            allergies=fv_list(data.get("allergies") or []),  # type: ignore[arg-type]
        ),
    )
    chief_complaint = fv(data.get("chief_complaint"))  # type: ignore[arg-type]
    if chief_complaint is not None:
        note.chief_complaint.CopyFrom(chief_complaint)
    hopi = fv(data.get("hopi"))  # type: ignore[arg-type]
    if hopi is not None:
        note.hopi.CopyFrom(hopi)
    examination_findings = fv(data.get("examination_findings"))  # type: ignore[arg-type]
    if examination_findings is not None:
        note.examination_findings.CopyFrom(examination_findings)
    treatment_plan = fv(data.get("treatment_plan"))  # type: ignore[arg-type]
    if treatment_plan is not None:
        note.treatment_plan.CopyFrom(treatment_plan)
    return note


__all__ = ["ExtractionResult", "run_extraction"]
