"""Shared by the tests that need Postgres. Without one they skip; CI's api job,
which has one, runs them."""

import asyncio

import psycopg
import pytest

from db import DSN


def _reachable() -> bool:
    try:
        psycopg.connect(DSN, connect_timeout=2).close()
        return True
    except psycopg.OperationalError:
        return False


requires_db = pytest.mark.skipif(not _reachable(), reason="no Postgres at AML_DSN")


class Queue:
    """Stands in for arq's pool: records what was enqueued."""

    def __init__(self, fail=False):
        self.jobs, self.fail = [], fail

    async def enqueue_job(self, *args):
        await asyncio.sleep(0)          # yield, as a real network call would
        if self.fail:
            raise ConnectionError("redis is down")
        self.jobs.append(args)
