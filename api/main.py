"""FastAPI. Returns a case_id immediately and the UI polls; a run takes 10-20s
and later waits at a human gate, so the request cannot block on it."""

import os

import psycopg
from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI, HTTPException
from typing import Literal

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
ALTER TABLE cases ADD COLUMN IF NOT EXISTS narrative jsonb;
ALTER TABLE cases ADD COLUMN IF NOT EXISTS verified boolean;
ALTER TABLE cases ADD COLUMN IF NOT EXISTS escalated boolean;
ALTER TABLE cases ADD COLUMN IF NOT EXISTS verification jsonb;
ALTER TABLE cases ADD COLUMN IF NOT EXISTS evidence jsonb;
"""

app = FastAPI(title="AML Investigation Copilot")


class CaseRequest(BaseModel):
    alert_id: int


class Decision(BaseModel):
    decision: Literal["approved", "rejected"]


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
            " evidence_count, error, narrative, verified, escalated, verification,"
            " evidence"
            " FROM cases WHERE case_id = %s", (case_id,))).fetchone()
    if row is None:
        raise HTTPException(404, "no such case")
    keys = ("case_id", "alert_id", "status", "typology", "confidence",
            "reasoning", "evidence_count", "error", "narrative", "verified",
            "escalated", "verification", "evidence")
    return dict(zip(keys, (str(row[0]), *row[1:])))


@app.get("/cases/{case_id}/subgraph")
async def subgraph(case_id: str):
    """The GNN neighbourhood behind this case, shaped for a force graph.

    risk_score is only known for transactions the model alerted on; a neighbour
    that scored below the threshold is not in the alerts table and comes back
    null. Scoring one here would mean loading the graph and the GAT into the API
    process, which is the worker's job -- see section 8.
    """
    async with await psycopg.AsyncConnection.connect(DSN) as conn:
        row = await (await conn.execute(
            "SELECT evidence FROM cases WHERE case_id = %s", (case_id,))).fetchone()
        if row is None:
            raise HTTPException(404, "no such case")
        if row[0] is None:
            raise HTTPException(409, "case has no evidence yet")
        evidence = row[0]

        attention = {v["txn_id"]: v["attention_weight"] for v in evidence.values()
                     if v["kind"] == "gnn_attention"}
        alert_id = next(v["txn_id"] for v in evidence.values() if v["kind"] == "alert")
        ids = sorted({*attention, alert_id})
        rows = await (await conn.execute(
            "SELECT t.txn_id, t.ts, t.src_account, t.dst_account, t.amount,"
            " t.currency, a.risk_score FROM transactions t"
            " LEFT JOIN alerts a ON a.txn_id = t.txn_id"
            " WHERE t.txn_id = ANY(%s)", (ids,))).fetchall()

    nodes = [{"txn_id": r[0], "timestamp": str(r[1]), "src_account": r[2],
              "dst_account": r[3], "amount": float(r[4]), "currency": r[5],
              "risk_score": None if r[6] is None else round(float(r[6]), 4),
              "attention_weight": attention.get(r[0]),
              "is_alert": r[0] == alert_id} for r in rows]
    links = [{"source": alert_id, "target": n["txn_id"],
              "value": n["attention_weight"]}
             for n in nodes if not n["is_alert"] and n["attention_weight"] is not None]
    return {"nodes": nodes, "links": links}


@app.post("/cases/{case_id}/decision", status_code=202)
async def decide(case_id: str, body: Decision):
    """Resume a run parked at the human gate. The graph has been waiting in
    Postgres since the interrupt, however long that took."""
    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as conn:
        row = await (await conn.execute(
            "SELECT status, alert_id FROM cases WHERE case_id = %s", (case_id,))).fetchone()
        if row is None:
            raise HTTPException(404, "no such case")
        if row[0] != "awaiting_review":
            raise HTTPException(409, f"case is {row[0]}, not awaiting_review")
    await app.state.redis.enqueue_job("run_case", case_id, row[1], body.decision)
    return {"case_id": case_id, "status": body.decision}
