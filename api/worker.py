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
from config import redis_dsn
from db import DSN

# Deployed, Redis is a sidecar in the api task and the worker reaches it by a
# Cloud Map name that appears only once that task is running -- ~70s after
# apply, measured. arq's default of 5 retries 1s apart gave up after 5s, so the
# first worker exited on "Name or service not known" and ECS replaced it. Two
# minutes covers the wait without a restart.
REDIS = RedisSettings.from_dsn(redis_dsn())
REDIS.conn_retries = 60
REDIS.conn_retry_delay = 2


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
    state = None                       # bound on the failure path too
    try:
        state = await asyncio.to_thread(_invoke, case_id, payload)
        row = (_status(state), state["typology"], state["confidence"],
               state["reasoning"], len(state["evidence"]),
               json.dumps(state.get("narrative")),
               json.dumps(state["evidence"], default=str), None, case_id)
    except Exception as error:                      # surface it, never hang
        row = ("failed", None, None, None, None, None, None,
               f"{type(error).__name__}: {error}", case_id)

    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as conn:
        await conn.execute(
            "UPDATE cases SET status=%s, typology=%s, confidence=%s, reasoning=%s,"
            " evidence_count=%s, narrative=%s, evidence=%s, error=%s"
            " WHERE case_id=%s", row)
        if isinstance(state, dict) and "verified" in state:
            await conn.execute(
                "UPDATE cases SET verified=%s, escalated=%s, verification=%s"
                " WHERE case_id=%s",
                (state["verified"], state.get("escalated", False),
                 json.dumps({"citation_failures": state.get("citation_failures", []),
                             "hallucinated_entities": state.get("hallucinated_entities", []),
                             "guidance_only": state.get("guidance_only", [])}),
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
