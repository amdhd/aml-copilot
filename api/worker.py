"""arq worker. Runs the LangGraph; a run takes 10-20s and later pauses
indefinitely at the human gate, which no blocking HTTP request can represent."""

import asyncio
import json
import os

import psycopg
from arq.connections import RedisSettings

from langgraph.types import Command

from agent.graph import build, checkpointer_cm
from agent.nodes.gather_context import _gnn
from ml.score_batch import DSN

REDIS = RedisSettings.from_dsn(os.environ.get("AML_REDIS", "redis://localhost:6379"))


def _invoke(case_id: str, payload) -> dict:
    with checkpointer_cm() as checkpointer:
        checkpointer.setup()
        graph = build(checkpointer)
        return graph.invoke(payload, {"configurable": {"thread_id": case_id}})


def _status(state: dict) -> str:
    """A run that stopped at the gate is waiting, not finished."""
    if state.get("__interrupt__"):
        return "awaiting_review"
    return state.get("decision") or "done"


async def run_case(ctx, case_id: str, alert_id: int, decision: str | None = None):
    payload = (Command(resume=decision) if decision
               else {"case_id": case_id, "alert_id": alert_id})
    try:
        state = await asyncio.to_thread(_invoke, case_id, payload)
        row = (_status(state), state["typology"], state["confidence"],
               state["reasoning"], len(state["evidence"]),
               json.dumps(state.get("narrative")), None, case_id)
    except Exception as error:                      # surface it, never hang
        row = ("failed", None, None, None, None, None,
               f"{type(error).__name__}: {error}", case_id)

    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as conn:
        await conn.execute(
            "UPDATE cases SET status=%s, typology=%s, confidence=%s, reasoning=%s,"
            " evidence_count=%s, narrative=%s, error=%s WHERE case_id=%s", row)
        if isinstance(state, dict) and "verified" in state:
            await conn.execute(
                "UPDATE cases SET verified=%s, escalated=%s, verification=%s"
                " WHERE case_id=%s",
                (state["verified"], state.get("escalated", False),
                 json.dumps({"citation_failures": state.get("citation_failures", []),
                             "hallucinated_entities": state.get("hallucinated_entities", [])}),
                 case_id))


async def startup(ctx):
    _gnn()                                          # load graph + model once
    print("worker ready: GNN loaded")


class WorkerSettings:
    functions = [run_case]
    on_startup = startup
    redis_settings = REDIS
    # One at a time: the provider's requests-per-minute ceiling is the real
    # constraint, not CPU. Raise this only with a paid tier.
    max_jobs = int(os.environ.get("AML_MAX_JOBS", "1"))
    job_timeout = 300
