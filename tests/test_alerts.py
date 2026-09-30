"""The queue shows each alert's latest case, so reopening it is a click on the
case rather than a second paid run. Needs Postgres, like test_create_case.py."""

import asyncio
import random
import uuid

import psycopg
import pytest

from api import main
from db import ALERTS_SCHEMA, DSN
from tests.pg import requires_db

pytestmark = requires_db


@pytest.fixture
def alert():
    """An alert above every real score, so it heads the queue."""
    txn_id = random.randrange(9 * 10**9, 10**10)
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(main.SCHEMA)
        conn.execute(ALERTS_SCHEMA)
        conn.execute("INSERT INTO alerts VALUES (%s, now(), 'a', 'b', 1, 'Euro', 'ACH',"
                     " 1.0, 'test', 0)", (txn_id,))
    yield txn_id
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DELETE FROM cases WHERE alert_id = %s", (txn_id,))
        conn.execute("DELETE FROM alerts WHERE txn_id = %s", (txn_id,))


def row_for(txn_id):
    return next(a for a in asyncio.run(main.alerts(limit=1000)) if a["alert_id"] == txn_id)


def open_case(txn_id, status, created_at):
    case_id = str(uuid.uuid4())
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("INSERT INTO cases (case_id, alert_id, status, created_at)"
                     " VALUES (%s, %s, %s, %s)", (case_id, txn_id, status, created_at))
    return case_id


def test_an_alert_without_a_case_says_so(alert):
    row = row_for(alert)
    assert (row["case_id"], row["case_status"]) == (None, None)


def test_an_alert_carries_its_latest_case(alert):
    open_case(alert, "failed", "2026-09-01")
    latest = open_case(alert, "approved", "2026-09-02")
    row = row_for(alert)
    assert (row["case_id"], row["case_status"]) == (latest, "approved")


def test_the_ground_truth_label_is_still_not_exposed(alert):
    assert "is_laundering" not in row_for(alert)
