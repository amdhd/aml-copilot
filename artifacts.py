"""Fetch the build artifacts that are too large to live in the image.

The graph cache is 1.84GB. Section 2 runs one image for both ECS services, so
baking it would make the API task pull 1.84GB it never opens -- and ECR stores
layers in S3 anyway, so baking does not avoid the transfer, it doubles it. The
graph lives in the section 9 persistent-layer bucket instead and the worker
pulls it once, on the way up.

Locally this does nothing: the file is already on disk, built by `make train`.
"""

import hashlib
import os
import re
from pathlib import Path

from config import DEPLOYED


def _expected_digest(key: str) -> str:
    """The key names the file's sha256 by its first 16 hex, as a directory --
    graph/<digest>/<file>, infra/ephemeral's graph_key."""
    for part in key.split("/")[:-1]:
        if re.fullmatch(r"[0-9a-f]{16}", part):
            return part
    raise RuntimeError(
        f"{key!r} carries no sha256 prefix directory (graph/<16 hex>/<file>), "
        f"so the download cannot be checked before torch.load unpickles it.")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def ensure(path, env_var: str) -> Path:
    """`path`, downloaded from the S3 uri in `env_var` if it is not there yet."""
    local = Path(path)
    if local.exists():
        return local

    uri = os.environ.get(env_var)
    if not uri:
        if DEPLOYED:
            raise RuntimeError(
                f"{local} is missing and {env_var} is not set. Deployed, this "
                f"artifact is fetched from S3; there is no CSV in the image to "
                f"rebuild it from.")
        return local                   # local: load() rebuilds it from the CSV
    if not uri.startswith("s3://"):
        raise RuntimeError(f"{env_var} must be an s3:// uri, got {uri!r}")

    import boto3                       # only the deployed path pays for this

    bucket, _, key = uri[len("s3://"):].partition("/")
    expected = _expected_digest(key)
    local.parent.mkdir(parents=True, exist_ok=True)
    # Download beside the target and rename. A task killed mid-download -- spot
    # reclaims the worker, section 8 -- would otherwise leave a truncated file
    # that looks complete to the next start, and torch.load would fail on it
    # somewhere far from the cause.
    partial = local.with_name(local.name + ".part")
    print(f"fetching {uri}")
    boto3.client("s3").download_file(bucket, key, str(partial))
    # torch.load(weights_only=False) is pickle: whatever is in this file runs.
    # The task role can only read the bucket, but anyone who can write to it
    # could otherwise put code in the worker. Checked before the rename, so a
    # file that fails is never left where load() would find it.
    actual = _sha256(partial)
    if not actual.startswith(expected):
        partial.unlink()
        raise RuntimeError(f"{uri} has sha256 {actual[:16]}..., but its key says "
                           f"{expected}. Refusing to load it.")
    partial.replace(local)
    print(f"fetched {local} ({local.stat().st_size / 1e9:.2f}GB)")
    return local
