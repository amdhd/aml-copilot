"""The human gate answers once.

These need Postgres, because the property under test is Postgres's: two
concurrent UPDATEs of one row serialise on its lock. A fake connection would
test the fake. Each test makes its own case row and deletes it after.
"""

import asyncio
import uuid
from types import SimpleNamespace

import psycopg
import pytest
from fastapi import HTTPException

from api import main
from db import DSN
from tests.pg import Queue, requires_db


pytestmark = requires_db


@pytest.fixture
def case():
    case_id = str(uuid.uuid4())
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(main.SCHEMA)
        conn.execute("INSERT INTO cases (case_id, alert_id, status)"
                     " VALUES (%s, 7, 'awaiting_review')", (case_id,))
    yield case_id
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DELETE FROM cases WHERE case_id = %s", (case_id,))


def status(case_id):
    with psycopg.connect(DSN) as conn:
        return conn.execute("SELECT status FROM cases WHERE case_id = %s",
                            (case_id,)).fetchone()[0]


def signed_in(reviewer):
    """What require_reviewer leaves on the request."""
    return SimpleNamespace(state=SimpleNamespace(reviewer=reviewer))


async def attempt(case_id, decision, reviewer="alice"):
    try:
        return await main.decide(case_id, main.Decision(decision=decision),
                                 signed_in(reviewer))
    except HTTPException as error:
        return error.status_code


def test_concurrent_decisions_resume_the_run_once(case):
    """Approve and reject at once: exactly one is accepted and enqueued, and the
    other is told it lost rather than that it was recorded."""
    main.app.state.redis = queue = Queue()

    async def both():
        return await asyncio.gather(attempt(case, "approved", "alice"),
                                    attempt(case, "rejected", "bob"))

    results = asyncio.run(both())
    accepted = [r for r in results if isinstance(r, dict)]
    assert len(accepted) == 1
    assert 409 in results
    assert queue.jobs == [("run_case", case, 7, accepted[0]["decision"])]
    assert status(case) == "resuming"


def test_a_second_decision_after_the_first_is_refused(case):
    main.app.state.redis = queue = Queue()
    asyncio.run(attempt(case, "approved"))
    assert asyncio.run(attempt(case, "rejected")) == 409
    assert len(queue.jobs) == 1


def test_a_failed_enqueue_leaves_the_case_open(case):
    """Claimed but never queued would strand the case in `resuming` forever."""
    main.app.state.redis = Queue(fail=True)
    with pytest.raises(ConnectionError):
        asyncio.run(main.decide(case, main.Decision(decision="approved"),
                                signed_in("alice")))
    assert status(case) == "awaiting_review"
    with psycopg.connect(DSN) as conn:
        assert conn.execute("SELECT decided_by, decided_at FROM cases"
                            " WHERE case_id = %s", (case,)).fetchone() == (None, None)


def test_the_decision_records_who_made_it_and_when(case):
    main.app.state.redis = Queue()
    asyncio.run(attempt(case, "approved", "alice"))
    with psycopg.connect(DSN) as conn:
        by, at = conn.execute("SELECT decided_by, decided_at FROM cases"
                              " WHERE case_id = %s", (case,)).fetchone()
    assert by == "alice" and at is not None


def test_an_unknown_case_is_404():
    main.app.state.redis = Queue()
    assert asyncio.run(attempt(str(uuid.uuid4()), "approved")) == 404
