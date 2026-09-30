"""Reviewer login. No database: TestClient outside a `with` block never runs
the startup hook, and every request here stops at the middleware or at a
route that does not touch Postgres."""

import base64

import pytest
from fastapi.testclient import TestClient

from api import auth, main

HASH = auth.hash_password("correct horse", iterations=1_000)   # fast in tests


def basic(username, password):
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "authenticate", auth.Authenticator({"alice": HASH}))
    return TestClient(main.app)


def test_a_password_checks_against_its_own_hash_only():
    assert auth.check_password("correct horse", HASH)
    assert not auth.check_password("wrong horse", HASH)
    assert HASH != auth.hash_password("correct horse", iterations=1_000)  # salted


def test_no_login_is_a_browser_prompt(client):
    response = client.get("/api/alerts")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Basic ")


@pytest.mark.parametrize("headers", [
    basic("alice", "wrong horse"),
    basic("mallory", "correct horse"),
    {"Authorization": "Basic not-base64!"},
    {"Authorization": "Bearer abc"},
])
def test_a_bad_login_is_refused_not_an_error(client, headers):
    assert client.get("/api/alerts", headers=headers).status_code == 401


def test_a_good_login_reaches_the_router(client):
    """A 404 from the router, not a 401 from the middleware."""
    assert client.get("/api/no-such-route", headers=basic("alice", "correct horse")
                      ).status_code == 404


def test_the_health_check_needs_no_login(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_a_passing_header_is_not_rehashed(monkeypatch):
    check = auth.Authenticator({"alice": HASH})
    header = basic("alice", "correct horse")["Authorization"]
    assert check(header) == "alice"
    monkeypatch.setattr(auth, "check_password", lambda *_: pytest.fail("rehashed"))
    assert check(header) == "alice"


def test_reviewers_are_required_when_deployed(monkeypatch):
    monkeypatch.setattr(auth, "DEPLOYED", True)
    monkeypatch.delenv("AML_REVIEWERS", raising=False)
    with pytest.raises(RuntimeError, match="set AML_REVIEWERS"):
        auth.load_reviewers()


def test_the_ssm_placeholder_fails_on_the_way_up(monkeypatch):
    monkeypatch.setenv("AML_REVIEWERS", "set-me-from-the-cli")
    with pytest.raises(RuntimeError, match="not JSON"):
        auth.load_reviewers()


def test_locally_no_reviewers_means_no_login(monkeypatch):
    monkeypatch.setattr(auth, "DEPLOYED", False)
    monkeypatch.delenv("AML_REVIEWERS", raising=False)
    assert auth.load_reviewers() is None
