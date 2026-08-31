import json

import pytest

from coda_eval.gold import ReferenceTurn
from coda_eval.metrics.hallucination import (
    AgreementResult,
    Claim,
    HallucinationJudgeError,
    HumanRating,
    Verdict,
    claims_from_note,
    hallucination_rate,
    inter_rater_agreement,
    judge_claims,
    load_human_ratings,
)
from nlp_service.llm.cassette import CassetteLLMClient, CassetteTurn


def test_claims_from_note_covers_scalar_and_list_fields_and_summary() -> None:
    fields = {
        "chief_complaint": {"value": "sore throat", "source_turn_ids": [0]},
        "hopi": None,
        "examination_findings": None,
        "treatment_plan": {"value": "rest and fluids", "source_turn_ids": [4]},
        "past_medical_history": [],
        "medications": [{"value": "paracetamol", "source_turn_ids": [2]}],
        "allergies": [],
        "provisional_diagnosis": [],
        "investigations_advised": [],
    }
    summary = "The patient has a sore throat. Advised rest and fluids."
    claims = claims_from_note(fields, summary, all_turn_ids=[0, 1, 2, 3, 4])

    ids = {c.claim_id for c in claims}
    assert "field:chief_complaint" in ids
    assert "field:treatment_plan" in ids
    assert "field:medications:0" in ids
    assert "summary:0" in ids
    assert "summary:1" in ids
    # null fields and empty lists contribute nothing
    assert not any(c.field == "hopi" for c in claims)
    assert not any(c.field == "allergies" for c in claims)

    # summary claims cite the union of what the field claims cited (0, 2, 4)
    # -- NOT all_turn_ids (1, 3 were never cited by anything) -- keeping the
    # judge request small (module docstring's TPM constraint).
    summary_claim = next(c for c in claims if c.claim_id == "summary:0")
    assert summary_claim.source_turn_ids == [0, 2, 4]


def test_claims_from_note_summary_falls_back_to_all_turn_ids_with_no_field_claims() -> None:
    fields = {
        "chief_complaint": None,
        "hopi": None,
        "examination_findings": None,
        "treatment_plan": None,
        "past_medical_history": [],
        "medications": [],
        "allergies": [],
        "provisional_diagnosis": [],
        "investigations_advised": [],
    }
    claims = claims_from_note(fields, "Nothing was extracted.", all_turn_ids=[0, 1, 2])
    assert len(claims) == 1
    assert claims[0].source_turn_ids == [0, 1, 2]


@pytest.mark.asyncio
async def test_judge_claims_parses_verdicts_in_order() -> None:
    claims = [
        Claim(
            claim_id="field:chief_complaint", field="chief_complaint",
            value="sore throat", source_turn_ids=[0],
        ),
        Claim(
            claim_id="field:medications:0", field="medications",
            value="penicillin", source_turn_ids=[2],
        ),
    ]
    response = {
        "verdicts": [
            {
                "claim_id": "field:chief_complaint",
                "supported": True,
                "rationale": "turn 0 states it",
            },
            {
                "claim_id": "field:medications:0",
                "supported": False,
                "rationale": "turn 2 never mentions penicillin",
            },
        ]
    }
    client = CassetteLLMClient(
        turns=[CassetteTurn(content=json.dumps(response), tokens_in=100, tokens_out=50)]
    )

    verdicts = await judge_claims(
        llm_client=client,
        model="openai/gpt-oss-20b",
        turns=[ReferenceTurn(turn_id=0, speaker="Doctor", text="hi")],
        claims=claims,
    )

    assert verdicts[0] == Verdict(
        claim_id="field:chief_complaint", supported=True, rationale="turn 0 states it"
    )
    assert verdicts[1].supported is False


@pytest.mark.asyncio
async def test_judge_claims_only_sends_cited_turns() -> None:
    """The whole point of the redesign (module docstring's TPM constraint):
    a 100-turn transcript with 2 cited claims should render only those 2
    turns into the prompt, not all 100."""
    claims = [Claim(claim_id="c1", field="x", value="v", source_turn_ids=[5])]
    turns = [ReferenceTurn(turn_id=i, speaker="Doctor", text=f"turn {i}") for i in range(100)]
    response = {"verdicts": [{"claim_id": "c1", "supported": True, "rationale": "r"}]}
    client = CassetteLLMClient(
        turns=[CassetteTurn(content=json.dumps(response), tokens_in=1, tokens_out=1)]
    )

    await judge_claims(llm_client=client, model="m", turns=turns, claims=claims)

    sent_transcript = client.calls[0]["user_prompt"]
    assert isinstance(sent_transcript, str)
    assert "turn 5" in sent_transcript
    assert "turn 6" not in sent_transcript
    assert "turn 0:" not in sent_transcript


@pytest.mark.asyncio
async def test_judge_claims_empty_input_makes_no_call() -> None:
    client = CassetteLLMClient(turns=[])
    verdicts = await judge_claims(llm_client=client, model="m", turns=[], claims=[])
    assert verdicts == []
    assert client.calls == []


@pytest.mark.asyncio
async def test_judge_claims_malformed_json_raises() -> None:
    claims = [Claim(claim_id="c1", field="x", value="v", source_turn_ids=[0])]
    client = CassetteLLMClient(turns=[CassetteTurn(content="not json", tokens_in=1, tokens_out=1)])
    with pytest.raises(HallucinationJudgeError):
        await judge_claims(llm_client=client, model="m", turns=[], claims=claims)


@pytest.mark.asyncio
async def test_judge_claims_missing_verdict_raises() -> None:
    claims = [
        Claim(claim_id="c1", field="x", value="v", source_turn_ids=[0]),
        Claim(claim_id="c2", field="x", value="v2", source_turn_ids=[0]),
    ]
    response = {"verdicts": [{"claim_id": "c1", "supported": True}]}
    client = CassetteLLMClient(
        turns=[CassetteTurn(content=json.dumps(response), tokens_in=1, tokens_out=1)]
    )
    with pytest.raises(HallucinationJudgeError):
        await judge_claims(llm_client=client, model="m", turns=[], claims=claims)


def test_hallucination_rate() -> None:
    verdicts = [
        Verdict(claim_id="a", supported=True, rationale=""),
        Verdict(claim_id="b", supported=False, rationale=""),
        Verdict(claim_id="c", supported=False, rationale=""),
        Verdict(claim_id="d", supported=True, rationale=""),
    ]
    assert hallucination_rate(verdicts) == 0.5


def test_hallucination_rate_empty_is_none() -> None:
    assert hallucination_rate([]) is None


def test_load_human_ratings_valid() -> None:
    data = [{"claim_id": "a", "supported": True}, {"claim_id": "b", "supported": False}]
    ratings = load_human_ratings(data)
    assert ratings == [
        HumanRating(claim_id="a", supported=True),
        HumanRating(claim_id="b", supported=False),
    ]


def test_load_human_ratings_rejects_malformed() -> None:
    with pytest.raises(ValueError):
        load_human_ratings([{"claim_id": "a"}])  # missing "supported"
    with pytest.raises(ValueError):
        load_human_ratings("not a list")  # type: ignore[arg-type]


def test_inter_rater_agreement_perfect_agreement() -> None:
    judge = [
        Verdict(claim_id="a", supported=True, rationale=""),
        Verdict(claim_id="b", supported=False, rationale=""),
    ]
    human = [HumanRating(claim_id="a", supported=True), HumanRating(claim_id="b", supported=False)]
    result = inter_rater_agreement(judge, human)
    assert result.n_compared == 2
    assert result.agreement_rate == 1.0
    # perfect agreement with both categories present -> kappa == 1.0
    assert result.cohens_kappa == pytest.approx(1.0)


def test_inter_rater_agreement_only_compares_shared_claim_ids() -> None:
    judge = [
        Verdict(claim_id="a", supported=True, rationale=""),
        Verdict(claim_id="b", supported=True, rationale=""),
        Verdict(claim_id="only_judge", supported=False, rationale=""),
    ]
    human = [
        HumanRating(claim_id="a", supported=True),
        HumanRating(claim_id="b", supported=False),
        HumanRating(claim_id="only_human", supported=True),
    ]
    result = inter_rater_agreement(judge, human)
    assert result.n_compared == 2
    assert result.agreement_rate == 0.5


def test_inter_rater_agreement_no_overlap() -> None:
    judge = [Verdict(claim_id="x", supported=True, rationale="")]
    human = [HumanRating(claim_id="y", supported=True)]
    result = inter_rater_agreement(judge, human)
    assert result.n_compared == 0
    assert result.cohens_kappa is None


def test_inter_rater_agreement_constant_rater_kappa_is_none() -> None:
    # Both raters say "supported" every time -> p_expected == 1.0, kappa undefined.
    judge = [
        Verdict(claim_id="a", supported=True, rationale=""),
        Verdict(claim_id="b", supported=True, rationale=""),
    ]
    human = [HumanRating(claim_id="a", supported=True), HumanRating(claim_id="b", supported=True)]
    result: AgreementResult = inter_rater_agreement(judge, human)
    assert result.agreement_rate == 1.0
    assert result.cohens_kappa is None
