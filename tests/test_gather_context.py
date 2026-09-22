"""txn_id is a position in the graph, so the graph and the database have to
agree about which transaction a position holds.

Nothing in the schema enforces that agreement -- it is a convention shared by
score_batch, load_transactions and the dataset loader. These tests cover the
check that turns a broken convention into an error instead of a plausible-
looking set of attention weights over the wrong neighbours.
"""

import numpy as np
import pytest
import torch
from torch_geometric.data import Data

from agent.nodes.gather_context import _check_node

AMOUNTS = [100.0, 9_567.48, 876_934_779.0]


def graph(amounts=AMOUNTS):
    """A stand-in graph whose column 0 is log1p(amount), as _features builds it."""
    x = torch.zeros(len(amounts), 47)
    x[:, 0] = torch.log1p(torch.tensor(amounts, dtype=torch.float32))
    return Data(x=x)


def test_matching_amount_passes():
    for i, amount in enumerate(AMOUNTS):
        _check_node(graph(), i, amount)


def test_float32_roundtrip_holds_at_large_amounts():
    """log1p/expm1 in float32 is lossy, so the tolerance has to survive the
    largest amounts in the dataset rather than just the typical ones."""
    data = graph()
    recovered = float(np.expm1(data.x[2, 0].item()))
    assert abs(recovered - AMOUNTS[2]) / AMOUNTS[2] < 1e-5


def test_txn_id_past_the_end_raises():
    with pytest.raises(ValueError, match="outside the 3-node graph"):
        _check_node(graph(), 3, 100.0)


def test_negative_txn_id_raises():
    with pytest.raises(ValueError, match="outside the 3-node graph"):
        _check_node(graph(), -1, 100.0)


def test_renumbered_graph_raises():
    """The failure this exists for: the id is in range, so indexing succeeds and
    returns a real transaction -- just not the one the database meant."""
    with pytest.raises(ValueError, match="different row orderings"):
        _check_node(graph(), 0, 9_567.48)     # node 0 holds 100.0
