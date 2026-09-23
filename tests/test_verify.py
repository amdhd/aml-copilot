"""The verifier is the point of the project, so it is the thing with tests.

It is pure: a dict in, a dict out, no LLM, no database, no model. Everything
else in agent/ needs a provider or Postgres to exercise; this does not.
"""

import pytest

from agent.verify import verify_citations


def bundle(*accounts):
    """An evidence bundle holding one transaction per account pair."""
    return {f"txn:{i}": {"kind": "transaction", "txn_id": i,
                         "src_account": src, "dst_account": dst}
            for i, (src, dst) in enumerate(accounts)}


def sentence(text, *ids):
    return {"text": text, "evidence_ids": list(ids)}


def test_clean_narrative_verifies():
    state = {"evidence": bundle(("14766-805C42580", "1299-8010F8CE0")),
             "narrative": [sentence("14766-805C42580 paid 1299-8010F8CE0.", "txn:0")]}
    result = verify_citations(state)
    assert result["verified"] is True
    assert result["citation_failures"] == []
    assert result["hallucinated_entities"] == []
    assert result["escalated"] is False


def test_unresolved_evidence_id_is_caught_with_its_sentence():
    state = {"evidence": bundle(("14766-805C42580", "1299-8010F8CE0")),
             "narrative": [sentence("Fine.", "txn:0"),
                           sentence("Invented.", "txn:999")]}
    result = verify_citations(state)
    assert result["verified"] is False
    assert result["citation_failures"] == [{"sentence": 1, "evidence_id": "txn:999"}]


def test_account_not_in_the_bundle_is_hallucinated():
    state = {"evidence": bundle(("14766-805C42580", "1299-8010F8CE0")),
             "narrative": [sentence("Funds moved to 99999-DEADBEEF.", "txn:0")]}
    result = verify_citations(state)
    assert result["verified"] is False
    assert result["hallucinated_entities"] == [
        {"sentence": 0, "account": "99999-DEADBEEF"}]


def test_both_failure_kinds_are_reported_together():
    state = {"evidence": bundle(("14766-805C42580", "1299-8010F8CE0")),
             "narrative": [sentence("To 99999-DEADBEEF.", "txn:404")]}
    result = verify_citations(state)
    assert len(result["citation_failures"]) == 1
    assert len(result["hallucinated_entities"]) == 1


def test_escalates_only_after_the_retry_is_spent():
    state = {"evidence": bundle(("14766-805C42580", "1299-8010F8CE0")),
             "narrative": [sentence("Bad.", "txn:404")], "draft_attempts": 1}
    assert verify_citations(state)["escalated"] is False
    assert verify_citations({**state, "draft_attempts": 2})["escalated"] is True


def test_a_passing_second_attempt_does_not_escalate():
    state = {"evidence": bundle(("14766-805C42580", "1299-8010F8CE0")),
             "narrative": [sentence("Fine.", "txn:0")], "draft_attempts": 2}
    result = verify_citations(state)
    assert result["verified"] is True
    assert result["escalated"] is False


def test_guidance_facts_carry_no_accounts_and_do_not_widen_the_known_set():
    """A guidance chunk has no src/dst, so citing it cannot launder an account
    into the allowed set."""
    evidence = {**bundle(("14766-805C42580", "1299-8010F8CE0")),
                "guidance:fincen#3": {"kind": "guidance", "text": "..."}}
    state = {"evidence": evidence,
             "narrative": [sentence("Per guidance, 99999-DEADBEEF is suspicious.",
                                    "guidance:fincen#3")]}
    result = verify_citations(state)
    assert result["hallucinated_entities"] == [
        {"sentence": 0, "account": "99999-DEADBEEF"}]


@pytest.mark.parametrize("text", [
    "Paid 886180041.84 Rupee.",          # a bare amount is not an account
    "On 2022-09-12 the funds moved.",    # nor is a date
    "Reference AB-123.",                 # too few characters after the dash
])
def test_prose_that_is_not_an_account_is_left_alone(text):
    state = {"evidence": bundle(("14766-805C42580", "1299-8010F8CE0")),
             "narrative": [sentence(text, "txn:0")]}
    assert verify_citations(state)["hallucinated_entities"] == []


def test_lowercase_account_is_detected():
    """A case-variant of an entity id is still an entity claim, so a lowercase
    account the model invented is caught the same as an uppercase one."""
    state = {"evidence": bundle(("14766-805C42580", "1299-8010F8CE0")),
             "narrative": [sentence("Funds moved to 99999-deadbeef.", "txn:0")]}
    assert verify_citations(state)["hallucinated_entities"] == [
        {"sentence": 0, "account": "99999-deadbeef"}]


def test_real_account_written_in_lowercase_is_not_hallucinated():
    """Comparison is case-insensitive: the same account in a different case is
    not a hallucination."""
    state = {"evidence": bundle(("14766-805C42580", "1299-8010F8CE0")),
             "narrative": [sentence("Funds moved to 14766-805c42580.", "txn:0")]}
    assert verify_citations(state)["hallucinated_entities"] == []


def test_empty_narrative_verifies_vacuously():
    """A case short-circuited at typology `none` never drafts. It must not be
    reported as a verification failure."""
    result = verify_citations({"evidence": {}, "narrative": []})
    assert result["verified"] is True
    assert result["escalated"] is False


GUIDANCE = {"guidance:ffiec#13": {"kind": "guidance", "source": "ffiec", "chunk": 13,
                                  "text": "Funds are moved through multiple accounts."}}


def test_sentence_resting_only_on_guidance_fails():
    state = {"evidence": {**bundle(("14766-805C42580", "1299-8010F8CE0")), **GUIDANCE},
             "narrative": [sentence("This is consistent with layering.", "guidance:ffiec#13")]}
    result = verify_citations(state)
    assert result["verified"] is False
    assert result["guidance_only"] == [{"sentence": 0}]
    assert result["citation_failures"] == []      # every id resolved; still not supported


def test_guidance_beside_case_evidence_verifies():
    state = {"evidence": {**bundle(("14766-805C42580", "1299-8010F8CE0")), **GUIDANCE},
             "narrative": [sentence("The transfer matches a red flag for layering.",
                                    "txn:0", "guidance:ffiec#13")]}
    result = verify_citations(state)
    assert result["verified"] is True
    assert result["guidance_only"] == []
