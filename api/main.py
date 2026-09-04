"""FastAPI. Returns a case_id immediately and the UI polls; a run takes 10-20s
and later waits at a human gate, so the request cannot block on it."""

import os

import psycopg
from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from ml.score_batch import DSN

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    case_id        uuid PRIMARY KEY,
    alert_id       bigint NOT NULL,
    status         text NOT NULL,
    typology       text,
    confidence     real,
    reasoning      text,
    evidence_count int,
    error          text,
    created_at     timestamptz NOT NULL DEFAULT now()
);
"""

app = FastAPI(title="AML Investigation Copilot")


class CaseRequest(BaseModel):
    alert_id: int


@app.on_event("startup")
async def startup():
    app.state.redis = await create_pool(
        RedisSettings.from_dsn(os.environ.get("AML_REDIS", "redis://localhost:6379")))
    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as conn:
        await conn.execute(SCHEMA)


@app.get("/alerts")
async def alerts(limit: int = 20):
    """The queue, highest model risk first. Test split only: train-period scores
    are in-sample and not honest."""
    async with await psycopg.AsyncConnection.connect(DSN) as conn:
        rows = await (await conn.execute(
            "SELECT txn_id, ts, src_account, dst_account, amount, currency,"
            " payment_format, risk_score, is_laundering FROM alerts"
            " WHERE split = 'test' ORDER BY risk_score DESC LIMIT %s", (limit,))).fetchall()
    keys = ("alert_id", "timestamp", "src_account", "dst_account", "amount",
            "currency", "payment_format", "risk_score", "is_laundering")
    return [dict(zip(keys, r)) for r in rows]


@app.post("/cases", status_code=202)
async def create_case(body: CaseRequest):
    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as conn:
        row = await (await conn.execute(
            "INSERT INTO cases (case_id, alert_id, status) "
            "VALUES (gen_random_uuid(), %s, 'queued') RETURNING case_id",
            (body.alert_id,))).fetchone()
    case_id = str(row[0])
    await app.state.redis.enqueue_job("run_case", case_id, body.alert_id)
    return {"case_id": case_id, "status": "queued"}


@app.get("/cases/{case_id}")
async def get_case(case_id: str):
    async with await psycopg.AsyncConnection.connect(DSN) as conn:
        row = await (await conn.execute(
            "SELECT case_id, alert_id, status, typology, confidence, reasoning,"
            " evidence_count, error FROM cases WHERE case_id = %s", (case_id,))).fetchone()
    if row is None:
        raise HTTPException(404, "no such case")
    keys = ("case_id", "alert_id", "status", "typology", "confidence",
            "reasoning", "evidence_count", "error")
    return dict(zip(keys, (str(row[0]), *row[1:])))
