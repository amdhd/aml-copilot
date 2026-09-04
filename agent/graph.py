"""LangGraph definition. Deterministic graph, fixed edges, not a ReAct loop --
a regulator-facing process has to run the same path every time.

Nodes 1-6. Week 6 adds the UI; nothing in this graph changes for it.
"""

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph

from agent.nodes.classify_typology import classify_typology
from agent.nodes.draft_narrative import draft_narrative
from agent.nodes.gather_context import gather_context
from agent.nodes.human_review import human_review
from agent.nodes.retrieve_guidance import retrieve_guidance
from agent.verify import verify_citations
from agent.state import CaseState
from ml.score_batch import DSN


def _after_verify(state) -> str:
    """Pass, or escalate after one retry. An unverified draft never ships
    silently -- it reaches the human carrying its own failure list."""
    return "review" if state["verified"] or state["escalated"] else "retry"


def _needs_narrative(state) -> str:
    """No typology means no suspicious pattern was found. Drafting a report
    anyway would manufacture suspicion the classifier did not find."""
    return "stop" if state["typology"] == "none" else "draft"


def build(checkpointer):
    graph = StateGraph(CaseState)
    graph.add_node("gather_context", gather_context)
    graph.add_node("classify_typology", classify_typology)
    graph.add_node("retrieve_guidance", retrieve_guidance)
    graph.add_node("draft_narrative", draft_narrative)
    graph.add_edge(START, "gather_context")
    graph.add_edge("gather_context", "classify_typology")
    graph.add_conditional_edges("classify_typology", _needs_narrative,
                                {"draft": "retrieve_guidance", "stop": END})
    graph.add_node("verify_citations", verify_citations)
    graph.add_node("human_review", human_review)
    graph.add_edge("retrieve_guidance", "draft_narrative")
    graph.add_edge("draft_narrative", "verify_citations")
    graph.add_conditional_edges("verify_citations", _after_verify,
                                {"retry": "draft_narrative", "review": "human_review"})
    graph.add_edge("human_review", END)
    return graph.compile(checkpointer=checkpointer)


def checkpointer_cm():
    """State lives in Postgres from day one, never in memory -- the human gate
    in week 5 pauses runs for hours or days, and spot workers get killed."""
    return PostgresSaver.from_conn_string(DSN)
