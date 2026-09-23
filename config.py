"""Configuration, and a refusal to guess at it outside local development.

Every AML_* variable had a localhost fallback. That is right for `make api` on
a laptop and wrong for a task definition: a mistyped secret ARN or a forgotten
environment block produces a container that boots green, passes its health
check, and then fails one case at a time against a database that was never
there. The failure surfaces as an application error hours later rather than as
a task that would not start.

Set AML_ENV to anything but "local" and the fallbacks become errors, raised at
import -- so the task dies on the way up, which is where a deploy problem
belongs.
"""

import os

ENV = os.environ.get("AML_ENV", "local")
DEPLOYED = ENV != "local"


def env(name: str, default: str) -> str:
    """The variable, or `default` -- but only when running locally."""
    value = os.environ.get(name)
    if value:
        return value
    if DEPLOYED:
        raise RuntimeError(
            f"{name} is not set and AML_ENV={ENV!r} is not local, so the "
            f"development default ({default!r}) will not be used. Set {name} "
            f"in the task definition.")
    return default


def redis_dsn() -> str:
    """A function, not a constant: evaluating it at import would make the seed
    loader and the offline scripts demand a queue they never talk to."""
    return env("AML_REDIS", "redis://localhost:6379")
