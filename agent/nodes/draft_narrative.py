"""Node 4: draft the SAR narrative. LLM, schema-validated, sentence-level cites."""

import json

from agent.llm import complete_json
from agent.schemas import Narrative

SYSTEM = """You draft Suspicious Activity Report narratives for a compliance analyst \
to review. You do not decide anything; a human approves or rejects your draft.

Rules, in order of importance:
- Every sentence must carry the evidence_ids it rests on. An id that is not a key \
in the evidence bundle is a failure, so copy ids exactly.
- The alerted transaction is keyed `alert:<txn_id>`. It is the only one keyed that \
way; every other transaction is `txn:<txn_id>`. Writing `txn:` for the alerted \
transaction cites an id that does not exist.
- State only what the evidence shows. Never introduce an account, amount, date or \
currency that does not appear in the evidence.
- Amounts carry currencies. Do not compare or aggregate across currencies, and do \
not apply a threshold from one currency to an amount in another.
- Describe the activity: who, what, when, where, and why it is suspicious.
- The REGULATORY GUIDANCE ids are red flags and indicators from FATF, FFIEC and \
FinCEN. When a sentence says why the activity is suspicious, cite the red flag it \
matches together with the case ids that show the pattern. Never cite guidance \
alone: a sentence whose only ids are guidance fails the whole draft. Cite a \
guidance id only if its text describes the pattern you are stating; guidance on \
how to write a report is not a red flag.
- Write about money and accounts, never about the detection model. Attention \
weights, risk scores and neighbourhood sizes are internal diagnostics; an analyst \
reading this needs the transaction pattern, not the model's arithmetic. Use \
gnn_attention evidence to decide which transactions matter, then write about those \
transactions and cite them.
- Say when a transfer is between the same account, and do not present it as \
movement of value between parties.
- Plain declarative sentences. No speculation, no legal conclusions, no filler.
- One claim per sentence, and keep every sentence under 300 characters. A sentence \
joining two claims with "and" or a semicolon is two sentences: split it and cite \
each half. A sentence over the limit fails the whole draft.

Return JSON: {"sentences": [{"text": "...", "evidence_ids": ["..."]}, ...]}"""


def draft_narrative(state: dict) -> dict:
    evidence = state["evidence"]
    guidance = {k: v for k, v in evidence.items() if v["kind"] == "guidance"}
    facts = {k: v for k, v in evidence.items() if v["kind"] != "guidance"}

    # [system][retrieved guidance][case data], never interleaved, so the prefix
    # stays stable and provider prompt caching can hit it.
    case_data = (
        "REGULATORY GUIDANCE — red flags, cite beside case evidence\n"
        + json.dumps(guidance, indent=1, default=str)
        + f"\n\nTYPOLOGY: {state['typology']} (confidence {state['confidence']})\n"
        + "\nEVIDENCE BUNDLE — case facts\n"
        + json.dumps(facts, indent=1, default=str))

    narrative, usage = complete_json(SYSTEM, case_data, Narrative, max_tokens=8000)
    return {
        "narrative": [s.model_dump() for s in narrative.sentences],
        "draft_attempts": state.get("draft_attempts", 0) + 1,
        "usage": {**state.get("usage", {}), "draft_narrative": usage},
    }
