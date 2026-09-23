"""Node 2: which laundering typology does this match? LLM, schema-validated."""

import json

from agent.llm import complete_json
from agent.schemas import TypologyVerdict

# Also node 3's retrieval query: guidance is looked up by what the typology
# means, not by its enum name.
DEFINITIONS = {
    "structuring": "breaking a large sum into amounts below a reporting threshold",
    "layering": "moving funds through intermediaries to obscure their origin",
    "smurfing": "many small deposits across multiple people or accounts",
    "trade_based": "value moved through mis-invoiced trade",
    "rapid_movement": "funds arriving and leaving an account within a short window",
    "none": "the evidence does not support any typology",
}

SYSTEM = """You are an AML analyst classifying a flagged transaction into one \
laundering typology.

""" + "\n".join(f"{k:<16}- {v}" for k, v in DEFINITIONS.items()) + """

Rules:
- Judge only from the evidence given. Do not invent accounts, amounts or dates.
- A high GNN risk score is not by itself a typology. Say none if the pattern is absent.
- Same-account transfers are routine bookkeeping and are rarely laundering.
- confidence is your certainty in the label, 0 to 1.

Return JSON: {"typology": <one of the six>, "confidence": <float>, \
"reasoning": "<under 600 chars, citing evidence ids>"}"""


def classify_typology(state: dict) -> dict:
    verdict, usage = complete_json(
        SYSTEM, json.dumps(state["evidence"], indent=1, default=str), TypologyVerdict)
    return {
        "typology": verdict.typology.value,
        "confidence": verdict.confidence,
        "reasoning": verdict.reasoning,
        "usage": {**state.get("usage", {}), "classify_typology": usage},
    }
