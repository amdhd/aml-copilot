"""Batch score every transaction and write model-originated alerts to Postgres.

Alerts originate from the model score, never hand-picked -- otherwise the demo
is fake. Train-period rows are in-sample and score optimistically, so the split
is recorded on every row and the demo queue should filter to 'test'.
"""

import argparse
from pathlib import Path

import numpy as np
import psycopg
import torch

from db import ALERTS_COPY, ALERTS_SCHEMA, DSN
from ml.dataset import Sampler, load, load_flat
from ml.model import GAT
from ml.train import FANOUT, evaluate


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="data/HI-Small_Trans.csv")
    ap.add_argument("--model", default=None)
    args = ap.parse_args()

    model_path = args.model or f"artifacts/model-{Path(args.csv).stem}.pt"
    # Self-generated checkpoint (train.py); not untrusted input.
    ckpt = torch.load(model_path, weights_only=False)  # nosec B614
    data = load(args.csv, categories=ckpt["categories"])
    model = GAT(ckpt["in_dim"], ckpt["hidden"])
    model.load_state_dict(ckpt["state_dict"])

    nodes = np.arange(data.num_nodes)
    print(f"scoring {len(nodes):,} transactions")
    _, prob = evaluate(model, Sampler(data, FANOUT), nodes)

    flagged = np.flatnonzero(prob >= ckpt["threshold"])
    df, split = load_flat(args.csv)
    names = np.array(["train", "val", "test"])[split]
    print(f"{len(flagged):,} alerts at threshold {ckpt['threshold']:.4f}")

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(ALERTS_SCHEMA)
        conn.execute("TRUNCATE alerts")
        with conn.cursor().copy(ALERTS_COPY) as copy:
            for i in flagged:
                r = df.iloc[i]
                copy.write_row((int(i), r["Timestamp"], r["_src"], r["_dst"],
                                float(r["Amount Paid"]), r["Payment Currency"],
                                r["Payment Format"], float(prob[i]), names[i],
                                int(r["Is Laundering"])))
        for row in conn.execute(
                "SELECT split, count(*), sum(is_laundering) FROM alerts "
                "GROUP BY split ORDER BY split"):
            print(f"  {row[0]:5s} {row[1]:7,} alerts, {row[2]:5,} real laundering")


if __name__ == "__main__":
    main()
