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


# --- Resolving is not supporting ----------------------------------------------
#
# Each of these narratives cites ids that all exist, and each passed before the
# verifier checked the sentence against the facts it cites.

FACTS = {
    "alert:7": {"kind": "alert", "txn_id": 7, "timestamp": "2022-09-01 00:01:00",
                "src_account": "28-8000B8AB0", "dst_account": "12-80A42BFB0",
                "amount": 408253.83, "currency": "Mexican Peso",
                "gnn_risk_score": 0.9312},
    "txn:3": {"kind": "transaction", "txn_id": 3, "timestamp": "2022-09-03 10:15:00",
              "src_account": "14766-805C42580", "dst_account": "1299-8010F8CE0",
              "amount": 19.28, "currency": "Euro"},
    "gnn:3": {"kind": "gnn_attention", "txn_id": 3, "attention_weight": 0.41},
    "gnn:subgraph:7": {"kind": "gnn_subgraph", "txn_id": 7, "risk_score": 0.9312,
                       "neighbourhood_size": 12},
    **GUIDANCE,
    "guidance:fincen#2": {"kind": "guidance", "source": "fincen", "chunk": 2,
                          "text": "Cash deposits kept just under $10,000."},
}


def verify(*sentences):
    return verify_citations({"evidence": FACTS, "narrative": list(sentences)})


def test_the_cited_facts_verify():
    result = verify(sentence("28-8000B8AB0 sent 408,253.83 Mexican Peso to "
                             "12-80A42BFB0 on 2022-09-01.", "alert:7"))
    assert result["verified"] is True
    assert result["unsupported"] == []


def test_an_invented_amount_on_a_real_citation_fails():
    result = verify(sentence("28-8000B8AB0 sent 9,000,000.00 Mexican Peso.", "alert:7"))
    assert result["verified"] is False
    assert result["unsupported"] == [
        {"sentence": 0, "kind": "amount", "value": "9,000,000.00"}]


def test_an_amount_from_a_fact_the_sentence_does_not_cite_fails():
    """19.28 is in the bundle, but on txn:3, not on the alert this cites."""
    result = verify(sentence("The alerted transfer was 19.28.", "alert:7"))
    assert result["unsupported"] == [{"sentence": 0, "kind": "amount", "value": "19.28"}]


@pytest.mark.parametrize("written", ["408,253.83", "408,253.8", "408,254", "408253.83"])
def test_an_amount_may_be_rounded_to_the_precision_written(written):
    assert verify(sentence(f"The transfer was {written}.", "alert:7"))["unsupported"] == []


def test_rounding_further_than_the_digits_written_fails():
    """408,000 claims precision to the unit, and the fact says 408,253.83."""
    result = verify(sentence("The transfer was 408,000.", "alert:7"))
    assert result["unsupported"] == [{"sentence": 0, "kind": "amount", "value": "408,000"}]


def test_a_count_is_not_taken_for_an_amount():
    assert verify(sentence("It was one of 3 transfers.", "alert:7"))["verified"] is True


def test_a_threshold_stated_in_cited_guidance_is_supported():
    result = verify(sentence("The 408,253.83 transfer is far above the $10,000 "
                             "threshold.", "alert:7", "guidance:fincen#2"))
    assert result["unsupported"] == []


def test_an_invented_date_on_a_real_citation_fails():
    result = verify(sentence("The alerted transfer happened on 2022-09-03.", "alert:7"))
    assert result["unsupported"] == [{"sentence": 0, "kind": "date", "value": "2022-09-03"}]


def test_an_account_from_a_fact_the_sentence_does_not_cite_is_unsupported():
    """Real account, wrong citation: not a hallucination, still not supported."""
    result = verify(sentence("14766-805C42580 received the alerted funds.", "alert:7"))
    assert result["hallucinated_entities"] == []
    assert result["unsupported"] == [
        {"sentence": 0, "kind": "account", "value": "14766-805C42580"}]


def test_an_invented_account_without_its_bank_prefix_is_hallucinated():
    result = verify(sentence("Funds moved to account 80DEAD99F.", "alert:7"))
    assert result["hallucinated_entities"] == [{"sentence": 0, "account": "80DEAD99F"}]


def test_a_real_account_without_its_bank_prefix_is_checked_like_one_with_it():
    assert verify(sentence("Account 8000B8AB0 sent the funds.", "alert:7"))["verified"] is True
    result = verify(sentence("Account 805C42580 sent the funds.", "alert:7"))
    assert result["unsupported"] == [
        {"sentence": 0, "kind": "account", "value": "805C42580"}]


@pytest.mark.parametrize("ids", [("gnn:3",), ("gnn:subgraph:7",),
                                 ("gnn:3", "guidance:ffiec#13")])
def test_a_sentence_resting_only_on_model_internals_fails(ids):
    result = verify(sentence("This transaction drew the model's attention.", *ids))
    assert result["verified"] is False
    assert result["no_case_fact"] == [{"sentence": 0}]


def test_model_internals_beside_a_case_fact_verify():
    result = verify(sentence("The alerted transfer is linked to an earlier one.",
                             "alert:7", "txn:3", "gnn:3"))
    assert result["verified"] is True
