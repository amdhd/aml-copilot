"""The graph cache is fetched rather than baked, so the fetch has to be honest
about the three ways it can fail before boto3 is ever reached.

Nothing here talks to S3. What matters is that a missing artifact is loud in the
place where it is unrecoverable (deployed, where there is no CSV to rebuild
from) and silent in the place where it is not (locally, where load() rebuilds).
"""

import pytest

import artifacts


def test_existing_file_is_returned_untouched(tmp_path):
    path = tmp_path / "graph.pt"
    path.write_bytes(b"x")
    assert artifacts.ensure(path, "AML_GRAPH_S3") == path


def test_missing_locally_is_not_an_error(tmp_path, monkeypatch):
    """load() rebuilds from the CSV, which is what a laptop has."""
    monkeypatch.setattr(artifacts, "DEPLOYED", False)
    monkeypatch.delenv("AML_GRAPH_S3", raising=False)
    path = tmp_path / "graph.pt"
    assert artifacts.ensure(path, "AML_GRAPH_S3") == path


def test_missing_when_deployed_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts, "DEPLOYED", True)
    monkeypatch.delenv("AML_GRAPH_S3", raising=False)
    with pytest.raises(RuntimeError, match="no CSV in the image"):
        artifacts.ensure(tmp_path / "graph.pt", "AML_GRAPH_S3")


def test_non_s3_uri_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("AML_GRAPH_S3", "https://example.com/graph.pt")
    with pytest.raises(RuntimeError, match="must be an s3:// uri"):
        artifacts.ensure(tmp_path / "graph.pt", "AML_GRAPH_S3")


def test_uri_splits_into_bucket_and_key(tmp_path, monkeypatch):
    """A key with slashes in it must stay whole."""
    seen = {}

    class FakeS3:
        def download_file(self, bucket, key, dest):
            seen.update(bucket=bucket, key=key)
            open(dest, "wb").write(b"graph")

    monkeypatch.setenv("AML_GRAPH_S3", "s3://aml-artifacts/graphs/HI-Small.pt")
    monkeypatch.setitem(__import__("sys").modules, "boto3",
                        type("m", (), {"client": staticmethod(lambda _: FakeS3())}))
    path = tmp_path / "graph.pt"
    artifacts.ensure(path, "AML_GRAPH_S3")

    assert seen == {"bucket": "aml-artifacts", "key": "graphs/HI-Small.pt"}
    assert path.read_bytes() == b"graph"
    # The partial file is renamed, never left behind looking complete.
    assert not path.with_name(path.name + ".part").exists()
