"""LangGraph definition. Deterministic graph, fixed edges, not a ReAct loop --
a regulator-facing process has to run the same path every time.

Week 3: nodes 1-2. Guidance retrieval, narrative, verifier and the human gate
are weeks 4-5.
"""

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph

from agent.nodes.classify_typology import classify_typology
from agent.nodes.gather_context import gather_context
from agent.state import CaseState
from ml.score_batch import DSN


def build(checkpointer):
    graph = StateGraph(CaseState)
    graph.add_node("gather_context", gather_context)
    graph.add_node("classify_typology", classify_typology)
    graph.add_edge(START, "gather_context")
    graph.add_edge("gather_context", "classify_typology")
    graph.add_edge("classify_typology", END)
    return graph.compile(checkpointer=checkpointer)


def checkpointer_cm():
    """State lives in Postgres from day one, never in memory -- the human gate
    in week 5 pauses runs for hours or days, and spot workers get killed."""
    return PostgresSaver.from_conn_string(DSN)
