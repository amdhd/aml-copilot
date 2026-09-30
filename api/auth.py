"""Per-reviewer basic auth, so an approval names the person who gave it.

Reviewers are AML_REVIEWERS: a JSON object of username -> PBKDF2 hash, which
the task definition reads from SSM. Deployed it is required; locally it is
optional and, when unset, the API runs without a login as it always has.

Basic auth sends the password on every request, so it is only as private as
the transport -- the ALB terminates HTTPS in front of it (infra/ephemeral).

    python -m api.auth alice bob    # prompts for each password, prints the JSON
"""

import base64
import binascii
import getpass
import hashlib
import hmac
import json
import os
import secrets
import sys

from config import DEPLOYED

ITERATIONS = 600_000        # OWASP's 2023 figure for PBKDF2-HMAC-SHA256


def hash_password(password: str, iterations: int = ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    scheme, iterations, salt, digest = stored.split("$")
    if scheme != "pbkdf2_sha256":
        raise ValueError(f"unknown hash scheme {scheme!r}")
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                    bytes.fromhex(salt), int(iterations))
    return hmac.compare_digest(candidate.hex(), digest)


def load_reviewers() -> dict | None:
    raw = os.environ.get("AML_REVIEWERS")
    if not raw:
        if DEPLOYED:
            raise RuntimeError("set AML_REVIEWERS in the task definition")
        return None
    try:
        reviewers = json.loads(raw)
    except json.JSONDecodeError:
        # A value set by hand but not with api.auth: fail on the way up, not
        # per request.
        raise RuntimeError("AML_REVIEWERS is not JSON -- set it with "
                           "`python -m api.auth <names>`") from None
    if not isinstance(reviewers, dict) or not reviewers:
        raise RuntimeError("AML_REVIEWERS must map at least one username to a hash")
    return reviewers


class Authenticator:
    """Checks an Authorization header against the reviewers.

    PBKDF2 is slow on purpose -- ~0.5s on a quarter vCPU -- and the UI polls
    every 2s, so a header that has passed is remembered, keyed by its sha256
    rather than its text. A failed one is not, so guessing stays slow.
    """

    def __init__(self, reviewers: dict):
        self.reviewers = reviewers
        self._passed: dict[str, str] = {}

    def __call__(self, header: str | None) -> str | None:
        if not header or not header.startswith("Basic "):
            return None
        key = hashlib.sha256(header.encode()).hexdigest()
        if key in self._passed:
            return self._passed[key]
        try:
            username, _, password = (base64.b64decode(header[6:], validate=True)
                                     .decode().partition(":"))
        except (binascii.Error, UnicodeDecodeError):
            return None
        stored = self.reviewers.get(username)
        if stored is None or not check_password(password, stored):
            return None
        self._passed[key] = username
        return username


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m api.auth <username> [<username> ...]")
    print(json.dumps({name: hash_password(getpass.getpass(f"password for {name}: "))
                      for name in sys.argv[1:]}))
