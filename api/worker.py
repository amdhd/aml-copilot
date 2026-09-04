"""arq worker. Runs the LangGraph; a run takes 10-20s and later pauses
indefinitely at the human gate, which no blocking HTTP request can represent."""

import asyncio
import os

import psycopg
from arq.connections import RedisSettings

from agent.graph import build, checkpointer_cm
from agent.nodes.gather_context import _gnn
from ml.score_batch import DSN

REDIS = RedisSettings.from_dsn(os.environ.get("AML_REDIS", "redis://localhost:6379"))


def _run(case_id: str, alert_id: int) -> dict:
    with checkpointer_cm() as checkpointer:
        checkpointer.setup()
        graph = build(checkpointer)
        return graph.invoke(
            {"case_id": case_id, "alert_id": alert_id},
            {"configurable": {"thread_id": case_id}})


async def run_case(ctx, case_id: str, alert_id: int):
    try:
        state = await asyncio.to_thread(_run, case_id, alert_id)
        row = ("done", state["typology"], state["confidence"], state["reasoning"],
               len(state["evidence"]), None, case_id)
    except Exception as error:                      # surface it, never hang
        row = ("failed", None, None, None, None, f"{type(error).__name__}: {error}", case_id)

    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as conn:
        await conn.execute(
            "UPDATE cases SET status=%s, typology=%s, confidence=%s, reasoning=%s,"
            " evidence_count=%s, error=%s WHERE case_id=%s", row)


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
