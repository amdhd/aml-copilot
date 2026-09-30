"""A failed run is recorded as failed, with only the error's type.

Needs Postgres for the cases row, like test_decision.py.
"""

import asyncio
import uuid

import psycopg
import pytest

from api import main, worker
from db import DSN
from tests.pg import requires_db

pytestmark = requires_db


@pytest.fixture
def case():
    case_id = str(uuid.uuid4())
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(main.SCHEMA)
        conn.execute("INSERT INTO cases (case_id, alert_id, status)"
                     " VALUES (%s, 7, 'queued')", (case_id,))
    yield case_id
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DELETE FROM cases WHERE case_id = %s", (case_id,))


def test_a_failure_stores_its_type_and_logs_the_rest(case, monkeypatch, capsys):
    def boom(*_):
        raise ConnectionError("could not reach db.internal.example:5432")

    monkeypatch.setattr(worker, "_invoke", boom)
    asyncio.run(worker.run_case({}, case, 7))
    with psycopg.connect(DSN) as conn:
        status, error = conn.execute("SELECT status, error FROM cases WHERE case_id = %s",
                                     (case,)).fetchone()
    assert (status, error) == ("failed", "ConnectionError")
    assert "db.internal.example" in capsys.readouterr().err
