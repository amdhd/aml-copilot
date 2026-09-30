"""Opening a case spends provider credit, so POST /cases decides what may open one.

Each test inserts its own alerts under ids far above the dataset's (5M rows)
and deletes them, and every case opened on them, after.
"""

import asyncio
import random

import psycopg
import pytest
from fastapi import HTTPException, Response
from pydantic import ValidationError

from api import main
from db import ALERTS_SCHEMA, DSN
from tests.pg import Queue, requires_db


@pytest.fixture
def alerts():
    """Two alerts: one the queue offers (test split), one it does not (train)."""
    test_id, train_id = (random.randrange(9 * 10**9, 10**10) for _ in range(2))
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(main.SCHEMA)
        conn.execute(ALERTS_SCHEMA)
        for txn_id, split in ((test_id, "test"), (train_id, "train")):
            conn.execute(
                "INSERT INTO alerts VALUES (%s, now(), 'a', 'b', 1, 'Euro', 'ACH',"
                " 0.9, %s, 0)", (txn_id, split))
    yield test_id, train_id
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DELETE FROM cases WHERE alert_id IN (%s, %s)", (test_id, train_id))
        conn.execute("DELETE FROM alerts WHERE txn_id IN (%s, %s)", (test_id, train_id))


async def open_case(alert_id):
    try:
        return await main.create_case(main.CaseRequest(alert_id=alert_id), Response())
    except HTTPException as error:
        return error.status_code


def cases_for(alert_id):
    with psycopg.connect(DSN) as conn:
        return conn.execute("SELECT status FROM cases WHERE alert_id = %s",
                            (alert_id,)).fetchall()


@requires_db
def test_an_alert_the_queue_does_not_offer_is_404(alerts):
    main.app.state.redis = queue = Queue()
    _, train_id = alerts
    assert asyncio.run(open_case(train_id)) == 404        # in-sample score
    assert asyncio.run(open_case(10**11)) == 404          # no alert at all
    assert queue.jobs == []


@requires_db
def test_an_open_case_is_returned_rather_than_run_again(alerts):
    main.app.state.redis = queue = Queue()
    test_id, _ = alerts
    first = asyncio.run(open_case(test_id))
    again = asyncio.run(open_case(test_id))
    assert again["case_id"] == first["case_id"]
    assert len(queue.jobs) == 1


@requires_db
def test_concurrent_opens_of_one_alert_make_one_case(alerts):
    main.app.state.redis = queue = Queue()
    test_id, _ = alerts

    async def both():
        return await asyncio.gather(open_case(test_id), open_case(test_id))

    first, second = asyncio.run(both())
    assert first["case_id"] == second["case_id"]
    assert len(cases_for(test_id)) == 1
    assert len(queue.jobs) == 1


@requires_db
def test_a_settled_case_does_not_block_a_new_one(alerts):
    main.app.state.redis = Queue()
    test_id, _ = alerts
    first = asyncio.run(open_case(test_id))
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("UPDATE cases SET status = 'done' WHERE case_id = %s",
                     (first["case_id"],))
    assert asyncio.run(open_case(test_id))["case_id"] != first["case_id"]


@requires_db
def test_a_full_queue_refuses_new_cases(alerts, monkeypatch):
    main.app.state.redis = queue = Queue()
    with psycopg.connect(DSN) as conn:
        queued = conn.execute(
            "SELECT count(*) FROM cases WHERE status = 'queued'").fetchone()[0]
    monkeypatch.setattr(main, "MAX_QUEUED", queued)
    assert asyncio.run(open_case(alerts[0])) == 429
    assert queue.jobs == []


@requires_db
def test_a_failed_enqueue_does_not_leave_an_open_case_behind(alerts):
    test_id, _ = alerts
    main.app.state.redis = Queue(fail=True)
    with pytest.raises(ConnectionError):
        asyncio.run(main.create_case(main.CaseRequest(alert_id=test_id), Response()))
    assert cases_for(test_id) == [("failed",)]
    main.app.state.redis = Queue()
    assert isinstance(asyncio.run(open_case(test_id)), dict)


@pytest.mark.parametrize("alert_id", [-1, 2**63])
def test_an_alert_id_outside_bigint_is_refused_before_postgres(alert_id):
    with pytest.raises(ValidationError):
        main.CaseRequest(alert_id=alert_id)
