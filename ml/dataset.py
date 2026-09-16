"""IBM AML CSV -> PyG graph. Transactions are nodes, not edges.

Edge rule (one rule, covers the typologies we care about): two transactions are
linked if they touch the same account and are adjacent in time on that account,
up to NEIGHBORS positions apart. Every transaction touches two accounts, so a
node gets temporal neighbours on both its sender and receiver side. This yields
pass-through chains (money in -> money out) and structuring bursts (repeated
small transfers on one account) while keeping edges O(n * NEIGHBORS) instead of
quadratic on hub accounts.
"""

from pathlib import Path

import numpy as np
import pandas as pd
import torch
from torch_geometric.data import Data

NEIGHBORS = 3
TRAIN_FRAC, VAL_FRAC = 0.70, 0.10


def _features(df: pd.DataFrame) -> torch.Tensor:
    paid = df["Amount Paid"].to_numpy(np.float32)
    recv = df["Amount Received"].to_numpy(np.float32)
    ts = df["Timestamp"]
    hour = ts.dt.hour.to_numpy(np.float32) * (2 * np.pi / 24)
    dow = ts.dt.dayofweek.to_numpy(np.float32) * (2 * np.pi / 7)

    numeric = np.column_stack([
        np.log1p(paid),
        np.log1p(recv),
        np.log1p(recv) - np.log1p(paid),
        (df["Payment Currency"] == df["Receiving Currency"]).to_numpy(np.float32),
        (df["From Bank"] == df["To Bank"]).to_numpy(np.float32),
        (df["_src"] == df["_dst"]).to_numpy(np.float32),
        np.sin(hour), np.cos(hour), np.sin(dow), np.cos(dow),
    ])
    cats = pd.get_dummies(
        df[["Payment Format", "Payment Currency", "Receiving Currency"]],
        dtype=np.float32,
    ).to_numpy()
    return torch.from_numpy(np.column_stack([numeric, cats]).astype(np.float32))


def _edge_index(df: pd.DataFrame) -> torch.Tensor:
    n = len(df)
    src_acct, dst_acct = df["_src"].to_numpy(), df["_dst"].to_numpy()
    acct = np.concatenate([src_acct, dst_acct])
    node = np.concatenate([np.arange(n), np.arange(n)])
    when = np.concatenate([df["_t"].to_numpy(), df["_t"].to_numpy()])

    # A same-account transfer touches one account, not two. Without this it
    # enters that account's timeline twice, pairs with itself, and those pairs
    # are dropped as self-loops -- leaving it with half the neighbours it should
    # have and 12% of them with none at all.
    keep = np.concatenate([np.ones(n, bool), src_acct != dst_acct])
    acct, node, when = acct[keep], node[keep], when[keep]

    order = np.lexsort((when, acct))
    acct, node = acct[order], node[order]

    src, dst = [], []
    for off in range(1, NEIGHBORS + 1):
        same = acct[:-off] == acct[off:]
        src.append(node[:-off][same])
        dst.append(node[off:][same])
    src, dst = np.concatenate(src), np.concatenate(dst)

    keep = src != dst
    src, dst = src[keep], dst[keep]
    both = np.stack([np.concatenate([src, dst]), np.concatenate([dst, src])])
    return torch.from_numpy(both).long()


def load(csv_path: str, max_rows: int | None = None) -> Data:
    # Building 61M edges over 5M rows takes minutes; every run reuses the cache.
    cache = Path(csv_path).with_suffix(f".{max_rows}.graph.pt")
    if cache.exists():
        # Self-generated graph cache; weights_only=True cannot load a PyG Data
        # object, and the file is never untrusted input.
        return torch.load(cache, weights_only=False)  # nosec B614

    df = pd.read_csv(csv_path, nrows=max_rows)
    df["Timestamp"] = pd.to_datetime(df["Timestamp"], format="%Y/%m/%d %H:%M")
    df = df.sort_values("Timestamp", kind="stable").reset_index(drop=True)
    df["_t"] = df["Timestamp"].astype("int64")

    # Account ids repeat across banks, so key on the pair.
    sender = df["From Bank"].astype(str) + "-" + df["Account"].astype(str)
    receiver = df["To Bank"].astype(str) + "-" + df["Account.1"].astype(str)
    codes, _ = pd.factorize(pd.concat([sender, receiver], ignore_index=True))
    df["_src"], df["_dst"] = codes[: len(df)], codes[len(df):]

    n = len(df)
    split = torch.zeros(n, dtype=torch.long)
    split[int(n * TRAIN_FRAC):] = 1
    split[int(n * (TRAIN_FRAC + VAL_FRAC)):] = 2

    data = Data(
        x=_features(df),
        edge_index=_edge_index(df),
        y=torch.from_numpy(df["Is Laundering"].to_numpy(np.int64).copy()),
        train_mask=split == 0,
        val_mask=split == 1,
        test_mask=split == 2,
    )
    torch.save(data, cache)
    return data


def load_flat(csv_path: str, max_rows: int | None = None):
    """Same rows and same temporal split, as a flat table for the baseline."""
    df = pd.read_csv(csv_path, nrows=max_rows)
    df["Timestamp"] = pd.to_datetime(df["Timestamp"], format="%Y/%m/%d %H:%M")
    df = df.sort_values("Timestamp", kind="stable").reset_index(drop=True)
    df["_src"] = df["From Bank"].astype(str) + "-" + df["Account"].astype(str)
    df["_dst"] = df["To Bank"].astype(str) + "-" + df["Account.1"].astype(str)
    n = len(df)
    split = np.full(n, 2)
    split[: int(n * TRAIN_FRAC)] = 0
    split[int(n * TRAIN_FRAC): int(n * (TRAIN_FRAC + VAL_FRAC))] = 1
    return df, split


class Sampler:
    """Neighbour sampler. PyG's own needs pyg-lib or torch-sparse, neither of
    which ships a macOS-arm64 wheel, and full-batch GAT does not fit past ~50k
    transactions. Deterministic (first k neighbours) when seed is None."""

    def __init__(self, data: Data, fanout: tuple[int, ...], seed: int | None = None):
        self.x, self.y = data.x, data.y
        self.fanout = fanout
        self.rng = np.random.default_rng(seed) if seed is not None else None
        row, col = data.edge_index.numpy()
        order = np.argsort(row, kind="stable")
        self.indices = col[order]
        self.indptr = np.zeros(data.num_nodes + 1, dtype=np.int64)
        np.cumsum(np.bincount(row, minlength=data.num_nodes), out=self.indptr[1:])

    def _hop(self, nodes, fanout):
        deg = self.indptr[nodes + 1] - self.indptr[nodes]
        k = np.minimum(deg, fanout)
        src = np.repeat(nodes, k)
        start = np.repeat(self.indptr[nodes], k)
        if self.rng is None:
            ends = np.cumsum(k)
            offset = np.arange(k.sum()) - np.repeat(ends - k, k)
        else:
            offset = (self.rng.random(k.sum()) * np.repeat(deg, k)).astype(np.int64)
        return src, self.indices[start + offset]

    def subgraph(self, seeds: np.ndarray):
        """Global node ids in the sampled neighbourhood, and edges relabelled
        into it. Explainability needs the ids; training does not."""
        frontier, edges = seeds, []
        for fanout in self.fanout:
            dst, src = self._hop(frontier, fanout)
            edges.append((src, dst))
            frontier = np.unique(src)
        src = np.concatenate([e[0] for e in edges])
        dst = np.concatenate([e[1] for e in edges])
        nodes = np.unique(np.concatenate([seeds, src, dst]))
        edge_index = np.stack([np.searchsorted(nodes, src), np.searchsorted(nodes, dst)])
        return nodes, torch.from_numpy(edge_index)

    def batch(self, seeds: np.ndarray):
        nodes, edge_index = self.subgraph(seeds)
        return (self.x[nodes],
                edge_index,
                torch.from_numpy(np.searchsorted(nodes, seeds)),
                self.y[seeds])
