"""Speaker role assignment: few-shot Groq classification of each diarized
cluster as Doctor/Patient (claude_context.md §4's structural-call bucket,
qwen/qwen3.6-27b as of decision #71 — llama-3.1-8b-instant was removed from
Groq's served model list), with a confidence score and an explicit uncertain
flag rather than a forced guess.

Both clusters are classified in one call, not independently — asking the
model to choose jointly is what lets it avoid assigning the same role to
both speakers, which independent per-cluster classification could not
detect or prevent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from asr_service.align import TurnDraft
from asr_service.groq_client import chat_json, parse_json_object
from coda.v1 import transcript_pb2
from coda_worker_sdk.errors import RetryableError

logger = logging.getLogger(__name__)

_SAMPLE_CHARS_PER_CLUSTER = 800
_MAX_TURNS_PER_CLUSTER_SAMPLE = 12

_SYSTEM_PROMPT = """You are classifying speakers in a doctor-patient outpatient \
consultation transcript. You are given short text samples from each \
diarized speaker cluster. For each cluster, decide whether that speaker is \
most likely the DOCTOR or the PATIENT, based on clinical framing (asking \
history/exam questions, giving diagnosis/advice) vs. reporting symptoms and \
answering questions.

Respond with a single JSON object, no prose outside it:
{"clusters": {"<cluster_id>": {"role": "doctor" | "patient" | "unknown", \
"confidence": <float 0-1>, "reasoning": "<one short sentence>"}, ...}}

Every cluster id given to you must appear as a key. Use "unknown" only if \
the sample truly gives no signal either way. Do not assign the same role to \
two different clusters unless you are using "unknown" for the ambiguous one."""


@dataclass(frozen=True, slots=True)
class RoleAssignment:
    role: transcript_pb2.SpeakerRole
    confidence: float
    uncertain: bool
    reasoning: str


_ROLE_MAP = {
    "doctor": transcript_pb2.SpeakerRole.SPEAKER_ROLE_DOCTOR,
    "patient": transcript_pb2.SpeakerRole.SPEAKER_ROLE_PATIENT,
    "unknown": transcript_pb2.SpeakerRole.SPEAKER_ROLE_UNKNOWN,
}


def _cluster_samples(turns: list[TurnDraft]) -> dict[str, str]:
    by_cluster: dict[str, list[str]] = {}
    for t in turns:
        by_cluster.setdefault(t.speaker, []).append(t.text)
    samples: dict[str, str] = {}
    for cluster_id, texts in by_cluster.items():
        joined = " ".join(texts[:_MAX_TURNS_PER_CLUSTER_SAMPLE])
        samples[cluster_id] = joined[:_SAMPLE_CHARS_PER_CLUSTER]
    return samples


async def assign_roles(
    turns: list[TurnDraft],
    *,
    api_key: str,
    model: str,
    confidence_threshold: float,
) -> tuple[dict[str, RoleAssignment], int, int]:
    """Returns (cluster_id -> RoleAssignment, tokens_in, tokens_out).

    A cluster with no representative text (shouldn't normally happen — every
    cluster in `turns` has at least one turn) or a response missing a
    cluster id defaults to UNKNOWN with confidence 0, flagged uncertain.
    """
    samples = _cluster_samples(turns)
    if not samples:
        return {}, 0, 0

    user_prompt = "\n\n".join(
        f'Cluster "{cid}" sample:\n"""{text}"""' for cid, text in samples.items()
    )

    result = await chat_json(
        api_key=api_key,
        model=model,
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=user_prompt,
    )
    parsed = parse_json_object(result.content)
    clusters_raw = parsed.get("clusters", {})
    if not isinstance(clusters_raw, dict):
        raise RetryableError(
            "groq role-classifier response missing 'clusters' object", code="GROQ_BAD_SHAPE"
        )

    assignments: dict[str, RoleAssignment] = {}
    seen_roles: dict[str, list[str]] = {}
    for cluster_id in samples:
        entry = clusters_raw.get(cluster_id)
        if not isinstance(entry, dict):
            logger.warning(
                "role classifier gave no entry for cluster, defaulting to unknown",
                extra={"extra_fields": {"cluster_id": cluster_id}},
            )
            assignments[cluster_id] = RoleAssignment(
                role=_ROLE_MAP["unknown"], confidence=0.0, uncertain=True, reasoning=""
            )
            continue
        role_str = str(entry.get("role", "unknown")).lower()
        role = _ROLE_MAP.get(role_str, _ROLE_MAP["unknown"])
        try:
            confidence = float(entry.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        reasoning = str(entry.get("reasoning", ""))
        assignments[cluster_id] = RoleAssignment(
            role=role, confidence=confidence, uncertain=confidence < confidence_threshold,
            reasoning=reasoning,
        )
        if role_str in ("doctor", "patient"):
            seen_roles.setdefault(role_str, []).append(cluster_id)

    # Two clusters both classified as the same non-unknown role: neither
    # assignment can be trusted, so both are downgraded to uncertain rather
    # than silently keeping a guess that is provably wrong for at least one.
    for role_str, cluster_ids in seen_roles.items():
        if len(cluster_ids) > 1:
            logger.warning(
                "role classifier assigned the same role to multiple clusters",
                extra={"extra_fields": {"role": role_str, "cluster_ids": cluster_ids}},
            )
            for cid in cluster_ids:
                a = assignments[cid]
                assignments[cid] = RoleAssignment(
                    role=a.role, confidence=a.confidence, uncertain=True, reasoning=a.reasoning
                )

    return assignments, result.tokens_in, result.tokens_out


__all__ = ["RoleAssignment", "assign_roles"]
