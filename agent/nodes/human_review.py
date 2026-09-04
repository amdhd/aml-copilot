"""Node 6: the human gate. The run stops here and waits.

State is already in Postgres via the checkpointer, so the case survives the
worker being killed, a spot interruption, or a reviewer taking three days.
"""

from langgraph.types import interrupt


def human_review(state: dict) -> dict:
    decision = interrupt({
        "typology": state["typology"],
        "confidence": state["confidence"],
        "narrative": state["narrative"],
        "verified": state["verified"],
        "escalated": state.get("escalated", False),
        "citation_failures": state.get("citation_failures", []),
        "hallucinated_entities": state.get("hallucinated_entities", []),
    })
    return {"decision": decision}
