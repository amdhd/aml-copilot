"""Node 5: citation verification. Deterministic Python, no LLM.

An LLM judging another LLM's citations is unfalsifiable and would not survive a
compliance review. Every evidence_id in the draft must resolve to a fact that
node 1 actually assembled, and every account named in prose must be one that
appears in the bundle. This node is the point of the project.
"""

import re

# Accounts in this dataset are "<bank>-<hex account>", e.g. 14766-805C42580.
# Anything of that shape appearing in narrative prose is a claim about an entity.
ACCOUNT = re.compile(r"\b\d{1,6}-[0-9A-F]{6,12}\b")
MAX_ATTEMPTS = 2


def verify_citations(state: dict) -> dict:
    evidence = state["evidence"]
    known = {fact[key] for fact in evidence.values()
             for key in ("src_account", "dst_account") if key in fact}

    unresolved, hallucinated = [], []
    for index, sentence in enumerate(state["narrative"]):
        for evidence_id in sentence["evidence_ids"]:
            if evidence_id not in evidence:
                unresolved.append({"sentence": index, "evidence_id": evidence_id})
        for account in ACCOUNT.findall(sentence["text"]):
            if account not in known:
                hallucinated.append({"sentence": index, "account": account})

    attempts = state.get("draft_attempts", 1)
    passed = not unresolved and not hallucinated
    return {
        "citation_failures": unresolved,
        "hallucinated_entities": hallucinated,
        "verified": passed,
        # One retry, then escalate flagged. Never silently ship an unverified draft.
        "escalated": not passed and attempts >= MAX_ATTEMPTS,
    }
