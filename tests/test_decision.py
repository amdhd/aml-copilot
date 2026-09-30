"""The human gate answers once.

These need Postgres, because the property under test is Postgres's: two
concurrent UPDATEs of one row serialise on its lock. A fake connection would
test the fake. Without a database they skip; CI's api job, which has one, runs
them. Each test makes its own case row and deletes it after.
"""

import asyncio
import uuid

import psycopg
import pytest
from fastapi import HTTPException

from api import main
from db import DSN


def _reachable() -> bool:
    try:
        psycopg.connect(DSN, connect_timeout=2).close()
        return True
    except psycopg.OperationalError:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="no Postgres at AML_DSN")


class Queue:
    """Stands in for arq's pool: records what was enqueued."""

    def __init__(self, fail=False):
        self.jobs, self.fail = [], fail

    async def enqueue_job(self, *args):
        await asyncio.sleep(0)          # yield, as a real network call would
        if self.fail:
            raise ConnectionError("redis is down")
        self.jobs.append(args)


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


async def attempt(case_id, decision):
    try:
        return await main.decide(case_id, main.Decision(decision=decision))
    except HTTPException as error:
        return error.status_code


def test_concurrent_decisions_resume_the_run_once(case):
    """Approve and reject at once: exactly one is accepted and enqueued, and the
    other is told it lost rather than that it was recorded."""
    main.app.state.redis = queue = Queue()

    async def both():
        return await asyncio.gather(attempt(case, "approved"),
                                    attempt(case, "rejected"))

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
        asyncio.run(main.decide(case, main.Decision(decision="approved")))
    assert status(case) == "awaiting_review"


def test_an_unknown_case_is_404():
    main.app.state.redis = Queue()
    assert asyncio.run(attempt(str(uuid.uuid4()), "approved")) == 404
