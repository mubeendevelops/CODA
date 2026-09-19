"""Converts a real `ClinicalNote` proto back into the value/source_turn_ids
dict shape `field_scoring`/`gold_schema` share — the inverse of
`nlp_service.extraction._to_clinical_note`. Shared by every arm's eval runner
(`baseline_eval.py`, `got_eval.py`): both arms end in the same `ClinicalNote`
shape (worker.py's module docstring), so one converter scores both.
"""

from __future__ import annotations

from coda.v1 import clinical_pb2


def note_to_fields_dict(note: clinical_pb2.ClinicalNote) -> dict[str, object]:
    def fv(value: clinical_pb2.FieldValue) -> dict[str, object] | None:
        if value.value == "":
            return None
        return {"value": value.value, "source_turn_ids": list(value.source_turn_ids)}

    def fv_list(values: list[clinical_pb2.FieldValue]) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for v in values:
            if v.value != "":
                out.append({"value": v.value, "source_turn_ids": list(v.source_turn_ids)})
        return out

    return {
        "chief_complaint": fv(note.chief_complaint) if note.HasField("chief_complaint") else None,
        "hopi": fv(note.hopi) if note.HasField("hopi") else None,
        "examination_findings": (
            fv(note.examination_findings) if note.HasField("examination_findings") else None
        ),
        "treatment_plan": fv(note.treatment_plan) if note.HasField("treatment_plan") else None,
        "past_medical_history": fv_list(list(note.past_medical_history)),
        "medications": fv_list(list(note.medications_allergies.medications)),
        "allergies": fv_list(list(note.medications_allergies.allergies)),
        "provisional_diagnosis": fv_list(list(note.provisional_diagnosis)),
        "investigations_advised": fv_list(list(note.investigations_advised)),
    }


__all__ = ["note_to_fields_dict"]
