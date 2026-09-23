"""The one-hot vocabulary is the thing that makes a model portable to a subset
of the CSV it was trained on.

`get_dummies` emits a column per category it *observes*, so a narrower CSV
produced a narrower -- and differently ordered -- feature matrix. These tests
pin the alignment, because the failure mode it replaces was a silent one: the
columns still had plausible values, they just meant different categories.
"""

import numpy as np
import pandas as pd
import pytest
import torch

from ml.dataset import _features, load

COLUMNS = ["Timestamp", "From Bank", "Account", "To Bank", "Account.1",
           "Amount Received", "Receiving Currency", "Amount Paid",
           "Payment Currency", "Payment Format", "Is Laundering"]


def raw(*rows):
    """Rows of (format, currency) in the IBM CSV schema, timestamps unparsed."""
    return pd.DataFrame(
        [["2022/09/01 00:%02d" % i, "10", f"{i:08X}", "11", f"{i + 99:08X}",
          100.0, cur, 100.0, cur, fmt, 0]
         for i, (fmt, cur) in enumerate(rows)],
        columns=COLUMNS)


def frame(*rows):
    """The same rows, prepared the way load() prepares them before _features."""
    df = raw(*rows)
    df["Timestamp"] = pd.to_datetime(df["Timestamp"], format="%Y/%m/%d %H:%M")
    df["_src"] = np.arange(len(df))
    df["_dst"] = np.arange(len(df)) + len(df)
    return df


def write_csv(path, *rows):
    raw(*rows).to_csv(path, index=False)
    return str(path)


def test_vocabulary_is_returned_and_ordered():
    _, categories = _features(frame(("Wire", "Euro"), ("ACH", "US Dollar")))
    assert categories == ["Payment Format_ACH", "Payment Format_Wire",
                          "Payment Currency_Euro", "Payment Currency_US Dollar",
                          "Receiving Currency_Euro",
                          "Receiving Currency_US Dollar"]


def test_subset_keeps_the_training_width_and_alignment():
    """The bug this replaces: a CSV missing a category used to yield a narrower
    matrix whose columns silently shifted meaning."""
    full = frame(("Wire", "Euro"), ("ACH", "US Dollar"), ("Cash", "Yuan"))
    _, categories = _features(full)

    subset = frame(("ACH", "US Dollar"))
    x, used = _features(subset, categories)

    assert used == categories
    assert x.shape[1] == 10 + len(categories)      # numeric block + vocabulary
    # The ACH column is hot and every other format column is cold, at the same
    # index it occupied in the full matrix.
    hot = {categories[i] for i in range(len(categories))
           if x[0, 10 + i] == 1.0}
    assert hot == {"Payment Format_ACH", "Payment Currency_US Dollar",
                   "Receiving Currency_US Dollar"}


def test_unseen_category_raises():
    _, categories = _features(frame(("ACH", "Euro")))
    with pytest.raises(ValueError, match="not trained on"):
        _features(frame(("Bitcoin", "Euro")), categories)


def test_cache_built_under_another_vocabulary_is_rebuilt(tmp_path, capsys):
    """A stale cache cannot be detected from the tensor alone, so the vocabulary
    rides along on the Data and a mismatch has to rebuild."""
    csv = write_csv(tmp_path / "t.csv", ("ACH", "Euro"), ("Wire", "US Dollar"),
                    ("Cash", "Yuan"), ("ACH", "Euro"))

    narrow = load(csv)                             # builds and caches
    assert (tmp_path / "t.None.graph.pt").exists()

    wider = narrow.categories + ["Payment Format_Cheque"]
    rebuilt = load(csv, categories=wider)

    assert "rebuilding" in capsys.readouterr().out
    assert rebuilt.categories == wider
    assert rebuilt.x.shape[1] == narrow.x.shape[1] + 1
    # The added category is absent from these rows, so its column is all zero.
    assert torch.equal(rebuilt.x[:, -1], torch.zeros(len(rebuilt.x)))


def test_matching_vocabulary_reuses_the_cache(tmp_path):
    csv = write_csv(tmp_path / "t.csv", ("ACH", "Euro"), ("Wire", "US Dollar"))
    first = load(csv)
    again = load(csv, categories=first.categories)
    assert torch.equal(first.x, again.x)
