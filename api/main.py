"""FastAPI. Returns a case_id immediately and the UI polls; a run takes 10-20s
and later waits at a human gate, so the request cannot block on it."""

import asyncio
import uuid
from pathlib import Path

import psycopg
from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.staticfiles import StaticFiles
from typing import Literal

from pydantic import BaseModel, Field

from api.auth import Authenticator, load_reviewers
from config import redis_dsn
from db import DSN

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
ALTER TABLE cases ADD COLUMN IF NOT EXISTS decided_by text;
ALTER TABLE cases ADD COLUMN IF NOT EXISTS decided_at timestamptz;
"""

app = FastAPI(title="AML Investigation Copilot")
UI = Path("ui/dist")
# Cases waiting for the worker. It runs one at a time (api/worker.py), so a
# queue this deep is already ten minutes of provider calls; past it, a new case
# is refused rather than queued behind a flood nobody will read.
MAX_QUEUED = 20
OPEN = ("queued", "resuming", "awaiting_review")
# None locally without AML_REVIEWERS: no login, as before. Deployed, required.
REVIEWERS = load_reviewers()
authenticate = Authenticator(REVIEWERS) if REVIEWERS else None


@app.middleware("http")
async def strip_api_prefix(request, call_next):
    """The UI calls /api/..., which the Vite proxy strips in development.
    Deployed there is no proxy -- this process serves the UI too (bottom of
    file) -- so the stripping happens here, and the browser sees one origin in
    both. Unprefixed paths still work, for curl and the eval harness."""
    path = request.scope["path"]
    if path == "/api" or path.startswith("/api/"):
        request.scope["path"] = path[len("/api"):] or "/"
    return await call_next(request)


# Registered after strip_api_prefix, so it runs before it and sees the raw path.
@app.middleware("http")
async def require_reviewer(request, call_next):
    """Every route, the UI included, needs a reviewer's login -- except the
    ALB's health check, which has none to give."""
    request.state.reviewer = None
    if authenticate is None or request.scope["path"] == "/healthz":
        return await call_next(request)
    reviewer = await asyncio.to_thread(authenticate,
                                       request.headers.get("authorization"))
    if reviewer is None:
        return Response(status_code=401, headers={
            "WWW-Authenticate": 'Basic realm="AML Copilot", charset="UTF-8"'})
    request.state.reviewer = reviewer
    return await call_next(request)


class CaseRequest(BaseModel):
    # alerts.txn_id is a bigint; a larger int reached Postgres and came back 500.
    alert_id: int = Field(ge=0, lt=2**63)


class Decision(BaseModel):
    decision: Literal["approved", "rejected"]


def _valid_case_id(raw: str) -> str:
    """Reject a malformed id before it reaches Postgres, where an invalid uuid
    would raise a 500 instead of a clean 404."""
    try:
        uuid.UUID(raw)
    except ValueError:
        raise HTTPException(404, "no such case")
    return raw


@app.on_event("startup")
async def startup():
    app.state.redis = await create_pool(RedisSettings.from_dsn(redis_dsn()))
    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as conn:
        await conn.execute(SCHEMA)


@app.get("/healthz")
async def healthz():
    """For the ALB. No login and no database, so a slow RDS cannot fail the
    check and cycle a healthy task."""
    return {"ok": True}


@app.get("/alerts")
async def alerts(limit: int = Query(20, ge=1, le=1000)):
    """The queue, highest model risk first. Test split only: train-period scores
    are in-sample and not honest."""
    async with await psycopg.AsyncConnection.connect(DSN) as conn:
        rows = await (await conn.execute(
            "SELECT txn_id, ts, src_account, dst_account, amount, currency,"
            " payment_format, risk_score FROM alerts"
            " WHERE split = 'test' ORDER BY risk_score DESC LIMIT %s", (limit,))).fetchall()
    # is_laundering is the ground-truth label; exposing it would hand the demo
    # analyst the answer the investigation is supposed to find.
    keys = ("alert_id", "timestamp", "src_account", "dst_account", "amount",
            "currency", "payment_format", "risk_score")
    return [dict(zip(keys, r)) for r in rows]


@app.get("/cases")
async def cases(limit: int = Query(50, ge=1, le=500)):
    """Cases already opened, newest first. A run parked at the gate waits hours
    or days (section 8), so it has to be findable later -- not only by whoever
    happened to open it."""
    async with await psycopg.AsyncConnection.connect(DSN) as conn:
        rows = await (await conn.execute(
            # jsonb_typeof, not IS NOT NULL: a case that short-circuits at
            # typology `none` stores json.dumps(None), which is the jsonb scalar
            # `null` -- present as far as SQL is concerned, and empty in fact.
            "SELECT case_id, alert_id, status, typology, created_at,"
            " jsonb_typeof(narrative) = 'array' AS has_narrative, verified"
            " FROM cases ORDER BY created_at DESC LIMIT %s", (limit,))).fetchall()
    keys = ("case_id", "alert_id", "status", "typology", "created_at",
            "has_narrative", "verified")
    return [dict(zip(keys, (str(r[0]), *r[1:]))) for r in rows]


@app.post("/cases", status_code=202)
async def create_case(body: CaseRequest, response: Response):
    """Every case spends provider credit, so only an alert the queue actually
    offers can open one, an alert gets one open case at a time, and the queue
    has a ceiling."""
    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as conn:
        # The same test-split filter as /alerts. A train-split score is
        # in-sample, and an id with no alert at all used to take a queue slot
        # and fail in the worker.
        alert = await (await conn.execute(
            "SELECT 1 FROM alerts WHERE txn_id = %s AND split = 'test'",
            (body.alert_id,))).fetchone()
        if alert is None:
            raise HTTPException(404, "no such alert")
        async with conn.transaction():
            # Serialise per alert, so two clicks cannot both find no open case.
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (body.alert_id,))
            existing = await (await conn.execute(
                "SELECT case_id, status FROM cases WHERE alert_id = %s"
                " AND status = ANY(%s) ORDER BY created_at DESC LIMIT 1",
                (body.alert_id, list(OPEN)))).fetchone()
            if existing is not None:
                response.status_code = 200
                return {"case_id": str(existing[0]), "status": existing[1]}
            queued = (await (await conn.execute(
                "SELECT count(*) FROM cases WHERE status = 'queued'")).fetchone())[0]
            if queued >= MAX_QUEUED:
                raise HTTPException(429, f"{queued} cases already queued; try later")
            row = await (await conn.execute(
                "INSERT INTO cases (case_id, alert_id, status) "
                "VALUES (gen_random_uuid(), %s, 'queued') RETURNING case_id",
                (body.alert_id,))).fetchone()
        case_id = str(row[0])
        try:
            await app.state.redis.enqueue_job("run_case", case_id, body.alert_id)
        except Exception:
            # A `queued` row with no job would be returned as the open case for
            # this alert forever.
            await conn.execute(
                "UPDATE cases SET status = 'failed', error = 'EnqueueError'"
                " WHERE case_id = %s", (case_id,))
            raise
    return {"case_id": case_id, "status": "queued"}


@app.get("/cases/{case_id}")
async def get_case(case_id: str):
    _valid_case_id(case_id)
    async with await psycopg.AsyncConnection.connect(DSN) as conn:
        row = await (await conn.execute(
            "SELECT case_id, alert_id, status, typology, confidence, reasoning,"
            " evidence_count, error, narrative, verified, escalated, verification,"
            " evidence, decided_by, decided_at"
            " FROM cases WHERE case_id = %s", (case_id,))).fetchone()
    if row is None:
        raise HTTPException(404, "no such case")
    keys = ("case_id", "alert_id", "status", "typology", "confidence",
            "reasoning", "evidence_count", "error", "narrative", "verified",
            "escalated", "verification", "evidence", "decided_by", "decided_at")
    return dict(zip(keys, (str(row[0]), *row[1:])))


@app.get("/cases/{case_id}/subgraph")
async def subgraph(case_id: str):
    """The GNN neighbourhood behind this case, shaped for a force graph.

    risk_score is only known for transactions the model alerted on; a neighbour
    that scored below the threshold is not in the alerts table and comes back
    null. Scoring one here would mean loading the graph and the GAT into the API
    process, which is the worker's job -- see section 8.
    """
    _valid_case_id(case_id)
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
        alert = next((v for v in evidence.values() if v["kind"] == "alert"), None)
        if alert is None:
            raise HTTPException(500, "case evidence has no alert fact")
        alert_id = alert["txn_id"]
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
async def decide(case_id: str, body: Decision, request: Request):
    """Resume a run parked at the human gate. The graph has been waiting in
    Postgres since the interrupt, however long that took."""
    _valid_case_id(case_id)
    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as conn:
        # Claim the gate in one statement. A SELECT then an enqueue let a double
        # click, or two reviewers, both pass the check: LangGraph resumed with
        # the first decision and ignored the second, while this endpoint told
        # both callers theirs was recorded. A concurrent UPDATE waits on the row
        # lock and then finds the status already moved, so exactly one wins.
        row = await (await conn.execute(
            "UPDATE cases SET status = 'resuming', decided_by = %s, decided_at = now()"
            " WHERE case_id = %s AND status = 'awaiting_review' RETURNING alert_id",
            (request.state.reviewer, case_id))).fetchone()
        if row is None:
            current = await (await conn.execute(
                "SELECT status FROM cases WHERE case_id = %s", (case_id,))).fetchone()
            if current is None:
                raise HTTPException(404, "no such case")
            raise HTTPException(409, f"case is {current[0]}, not awaiting_review")
        try:
            await app.state.redis.enqueue_job("run_case", case_id, row[0], body.decision)
        except Exception:
            # Unclaimed, or the case would sit in `resuming` with no job behind it.
            await conn.execute(
                "UPDATE cases SET status = 'awaiting_review', decided_by = NULL,"
                " decided_at = NULL WHERE case_id = %s", (case_id,))
            raise
    return {"case_id": case_id, "status": "resuming", "decision": body.decision}


# Last, so every API route above matches first. Only when the UI has been built:
# locally `npm run dev` serves it, and the image builds it (Dockerfile).
if UI.is_dir():
    app.mount("/", StaticFiles(directory=UI, html=True), name="ui")
