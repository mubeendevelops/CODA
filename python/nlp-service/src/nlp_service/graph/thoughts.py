"""GoT-HCS Module 1 — thought construction, adapted for dialogue.

The base paper builds thoughts from prose discharge notes with trained
BioClinicalBERT projection heads. We have neither the labeled data nor the
prose: our input is turn-based conversational speech carrying disfluency,
interruption, and information that no single turn states on its own
(claude_context.md §6's "Thought construction" row, hypothesis H3). This
module is the prompted-LLM substitute, producing the same tuple the paper's
head produces plus the two fields dialogue forces on us:

    (thought_id, speaker, text_span, turn_id, entities, clinical_category,
     temporal_anchor, polarity, confidence)

`polarity` is the load-bearing addition. In a discharge note a clinician has
already resolved contradictions before writing; in a consultation they have
not. A symptom the patient asserts in turn 3 and the doctor negates in turn 9
must survive as **two** thoughts — one `asserted`, one `negated` — linked by a
negation edge (edges.py), never as one thought silently overwritten by the
later one. Resolving the contradiction here would destroy the very evidence
the graph exists to reason over, and would make "no chest pain" indistinguish-
able from "chest pain" by the time distillation runs.

`text_span` is the other addition: a single turn routinely yields several
thoughts, so citing the turn is not adequate provenance. The span is a
verbatim `[char_start, char_end)` slice of the turn's redacted text, enforced
by graph/schema.py rather than trusted.

Model role: thought construction runs on `RunConfig.base_model`, the same
model the baseline arm reads the transcript with, so the GoT-vs-baseline
comparison isolates graph reasoning rather than confounding it with a model
swap (claude_context.md §4's model-assignment table; edge prediction uses
`structural_model` instead — see edges.py).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import psycopg

from coda.v1 import thought_pb2, transcript_pb2
from coda_worker_sdk.errors import FatalError
from nlp_service import prompts
from nlp_service.graph import schema
from nlp_service.llm.client import LLMClient
from nlp_service.llm.limits import OTPM_SAFE_MAX_TOKENS
from nlp_service.llm_cache import complete_cached

logger = logging.getLogger(__name__)

## max_tokens history (2026-09-05): first scaled up per-turn to stop hidden
## reasoning from starving the answer (see nlp_service.llm.limits' docstring
## for the full story), then found that a large max_tokens can itself be
## rejected by Groq's separate OTPM per-minute ceiling — the fix for THAT
## problem (`reasoning_effort: "none"` at the client level) already removed
## the reason to request a large ceiling, so this now uses
## `nlp_service.llm.limits.OTPM_SAFE_MAX_TOKENS` directly instead of scaling.

_CATEGORY_BY_NAME = {
    "symptom": thought_pb2.ThoughtCategory.THOUGHT_CATEGORY_SYMPTOM,
    "history": thought_pb2.ThoughtCategory.THOUGHT_CATEGORY_HISTORY,
    "medication": thought_pb2.ThoughtCategory.THOUGHT_CATEGORY_MEDICATION,
    "allergy": thought_pb2.ThoughtCategory.THOUGHT_CATEGORY_ALLERGY,
    "examination": thought_pb2.ThoughtCategory.THOUGHT_CATEGORY_EXAMINATION,
    "diagnosis": thought_pb2.ThoughtCategory.THOUGHT_CATEGORY_DIAGNOSIS,
    "investigation": thought_pb2.ThoughtCategory.THOUGHT_CATEGORY_INVESTIGATION,
    "plan": thought_pb2.ThoughtCategory.THOUGHT_CATEGORY_PLAN,
    "other": thought_pb2.ThoughtCategory.THOUGHT_CATEGORY_OTHER,
}

_POLARITY_BY_NAME = {
    "asserted": thought_pb2.Polarity.POLARITY_ASSERTED,
    "negated": thought_pb2.Polarity.POLARITY_NEGATED,
    "uncertain": thought_pb2.Polarity.POLARITY_UNCERTAIN,
    "hypothetical": thought_pb2.Polarity.POLARITY_HYPOTHETICAL,
}

CATEGORY_NAME = {v: k for k, v in _CATEGORY_BY_NAME.items()}
POLARITY_NAME = {v: k for k, v in _POLARITY_BY_NAME.items()}

_SPEAKER_LABELS = {
    transcript_pb2.SpeakerRole.SPEAKER_ROLE_DOCTOR: "doctor",
    transcript_pb2.SpeakerRole.SPEAKER_ROLE_PATIENT: "patient",
    transcript_pb2.SpeakerRole.SPEAKER_ROLE_UNKNOWN: "unknown",
}
SPEAKER_NAME = _SPEAKER_LABELS


@dataclass(frozen=True, slots=True)
class ThoughtConstructionResult:
    thoughts: list[thought_pb2.Thought]
    tokens_in: int = 0
    tokens_out: int = 0
    llm_calls: int = 0
    cache_hits: int = 0
    schema_valid: bool = True
    repair_attempts: int = 0
    dropped: list[str] = field(default_factory=list)
    """Post-validation drops, each with a reason. Non-empty is not a failure
    — it is the record of what the model produced that survived JSON
    validation but not the semantic checks below. Logged, and surfaced so a
    caller can report it rather than have it vanish."""


def turn_texts(transcript: transcript_pb2.Transcript) -> dict[int, str]:
    """The exact per-turn text the prompt shows the model, keyed by
    turn_index. `text_redacted` always, never `text` — ADR-0014: only
    redacted text may reach a third-party LLM, and the span offsets stored
    on a thought must index the same string the model was shown, or the
    provenance claim is meaningless.
    """
    return {t.turn_index: (t.text_redacted or t.text) for t in transcript.turns}


def render_turns_for_construction(transcript: transcript_pb2.Transcript) -> str:
    lines = []
    for turn in transcript.turns:
        speaker = _SPEAKER_LABELS.get(turn.speaker_label, "unknown")
        lines.append(f"{turn.turn_index}: {speaker}: {turn.text_redacted or turn.text}")
    return "\n".join(lines)


async def construct_thoughts(
    *,
    conn: psycopg.AsyncConnection,
    llm_client: LLMClient,
    model: str,
    transcript: transcript_pb2.Transcript,
    consultation_id: str,
    run_config_id: str,
    turn_id_by_index: dict[int, str],
    repair_max_attempts: int,
    timeout_s: float,
    language: str = prompts.DEFAULT_LANGUAGE,
) -> ThoughtConstructionResult:
    """One prompted call over the whole transcript, with the same bounded
    repair loop `extraction.py` uses.

    The call is whole-transcript rather than per-turn on purpose. Per-turn
    calls would cost one request per turn (a 10-minute consultation is
    routinely 60+ turns, which alone would exceed the per-consultation token
    budget in claude_context.md §8), and — more importantly — would make the
    cross-turn cases structurally impossible: a model shown only turn 8
    cannot know that turn 6 was interrupted, and a model shown only turn 9
    cannot know it is negating turn 3.

    `turn_id_by_index` maps the transcript's turn_index to the `turns.id`
    UUID the thought's foreign key must carry. A thought citing a turn index
    with no database row is dropped rather than persisted with a dangling
    reference.
    """
    pr = prompts.load_thought_construction_prompts(language=language)
    texts = turn_texts(transcript)
    if not texts:
        raise FatalError(
            "thought construction received a transcript with no turns",
            code="EMPTY_TRANSCRIPT",
        )

    rendered = render_turns_for_construction(transcript)
    system_prompt = pr.system
    base_user = pr.user_template.format(transcript_turns=rendered)
    user_prompt = base_user

    tokens_in = tokens_out = cache_hits = repair_attempts = 0
    last_error = ""
    last_content = ""
    max_tokens = OTPM_SAFE_MAX_TOKENS

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
            max_tokens=max_tokens,
        )
        tokens_in += completion.tokens_in
        tokens_out += completion.tokens_out
        if cache_hit:
            cache_hits += 1

        result = schema.validate_thought_construction(completion.content, turn_texts=texts)
        if result.valid:
            thoughts, dropped = _to_thoughts(
                result.items,
                turn_texts=texts,
                speaker_by_index=speaker_by_index(transcript),
                turn_id_by_index=turn_id_by_index,
                consultation_id=consultation_id,
                run_config_id=run_config_id,
            )
            if dropped:
                logger.warning(
                    "thought construction dropped thoughts after validation",
                    extra={
                        "extra_fields": {
                            "consultation_id": consultation_id,
                            "dropped_count": len(dropped),
                            "reasons": dropped,
                        }
                    },
                )
            logger.info(
                "thought construction complete",
                extra={
                    "extra_fields": {
                        "consultation_id": consultation_id,
                        "run_config_id": run_config_id,
                        "thought_count": len(thoughts),
                        "turn_count": len(texts),
                        "repair_attempts": repair_attempts,
                    }
                },
            )
            return ThoughtConstructionResult(
                thoughts=thoughts,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                llm_calls=attempt + 1,
                cache_hits=cache_hits,
                schema_valid=True,
                repair_attempts=repair_attempts,
                dropped=dropped,
            )

        last_error = result.error
        last_content = completion.content
        logger.warning(
            "thought construction validation failed",
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
        user_prompt = base_user + pr.repair_addendum_template.format(
            validation_error=last_error, previous_response=last_content
        )

    # Same reporting caveat as extraction.py: StageWorker's exception
    # boundary discards StageMetrics on a raised WorkerError, so these
    # counters reach the structured log and nowhere else.
    logger.error(
        "thought construction exhausted repair budget without valid output",
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
        f"thought construction did not produce valid output after {repair_attempts} "
        f"repair attempt(s); last error: {last_error}",
        code="THOUGHT_CONSTRUCTION_INVALID",
    )


def _entity_list(item: dict[str, object]) -> list[object]:
    entities = item.get("entities")
    return list(entities) if isinstance(entities, list) else []


def _to_thoughts(
    items: list[dict[str, object]],
    *,
    turn_texts: dict[int, str],
    speaker_by_index: dict[int, transcript_pb2.SpeakerRole],
    turn_id_by_index: dict[int, str],
    consultation_id: str,
    run_config_id: str,
) -> tuple[list[thought_pb2.Thought], list[str]]:
    """Ids are assigned here, deterministically as `t{n}` in emission order,
    not by the model. A model-chosen id is one more thing that can collide
    or drift between the construction call and the edge call; a positional
    id cannot. The database assigns the real UUID on insert (store.py),
    which is why these stay stable only within one graph build.
    """
    thoughts: list[thought_pb2.Thought] = []
    dropped: list[str] = []

    for item in items:
        turn_index = int(item["turn_index"])  # type: ignore[call-overload]
        turn_id = turn_id_by_index.get(turn_index, "")
        if not turn_id:
            dropped.append(
                f"turn_index {turn_index} has no persisted turn row; thought dropped "
                f"rather than written with a dangling turn_id"
            )
            continue
        span = str(item["text_span"])
        located = schema.locate_span(turn_texts[turn_index], span)
        if located is None:
            # Unreachable via validate_thought_construction, which already
            # rejects this — kept because _to_thoughts is also called
            # directly by tests and by any future non-LLM thought source.
            dropped.append(f"text_span not locatable in turn {turn_index}: {span!r}")
            continue
        char_start, char_end = located
        anchor = item.get("temporal_anchor")
        thoughts.append(
            thought_pb2.Thought(
                id=f"t{len(thoughts) + 1}",
                consultation_id=consultation_id,
                run_config_id=run_config_id,
                turn_id=turn_id,
                turn_index=turn_index,
                speaker=speaker_by_index.get(
                    turn_index, transcript_pb2.SpeakerRole.SPEAKER_ROLE_UNKNOWN
                ),
                text=str(item["text"]),
                entities=[
                    thought_pb2.LinkedEntity(text=str(e)) for e in _entity_list(item)
                ],
                category=_CATEGORY_BY_NAME[str(item["clinical_category"])],
                temporal_anchor="" if anchor is None else str(anchor),
                polarity=_POLARITY_BY_NAME[str(item["polarity"])],
                confidence=float(item["confidence"]),  # type: ignore[arg-type]
                char_start=char_start,
                char_end=char_end,
            )
        )
    return thoughts, dropped


def speaker_by_index(
    transcript: transcript_pb2.Transcript,
) -> dict[int, transcript_pb2.SpeakerRole]:
    """Speaker is a property of the turn, taken from the diarizer's output —
    never asked of the construction model. Asking would introduce a way for
    the model to disagree with the diarizer about who said what, and the
    doctor-asserts/patient-denies distinction is exactly what the negation
    handling depends on being right.
    """
    return {t.turn_index: t.speaker_label for t in transcript.turns}


__all__ = [
    "ThoughtConstructionResult",
    "construct_thoughts",
    "speaker_by_index",
    "turn_texts",
    "render_turns_for_construction",
    "CATEGORY_NAME",
    "POLARITY_NAME",
    "SPEAKER_NAME",
]
