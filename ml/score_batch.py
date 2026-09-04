"""Batch score every transaction and write model-originated alerts to Postgres.

Alerts originate from the model score, never hand-picked -- otherwise the demo
is fake. Train-period rows are in-sample and score optimistically, so the split
is recorded on every row and the demo queue should filter to 'test'.
"""

import argparse
import os
from pathlib import Path

import numpy as np
import psycopg
import torch

from ml.dataset import Sampler, load, load_flat
from ml.model import GAT
from ml.train import FANOUT, evaluate

DSN = os.environ.get("AML_DSN", "postgresql://aml:aml@localhost:5432/aml")

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    txn_id         bigint PRIMARY KEY,
    ts             timestamp NOT NULL,
    src_account    text NOT NULL,
    dst_account    text NOT NULL,
    amount         numeric NOT NULL,
    currency       text NOT NULL,
    payment_format text NOT NULL,
    risk_score     real NOT NULL,
    split          text NOT NULL,
    is_laundering  smallint NOT NULL
);
CREATE INDEX IF NOT EXISTS alerts_risk ON alerts (risk_score DESC);
CREATE INDEX IF NOT EXISTS alerts_src  ON alerts (src_account);
CREATE INDEX IF NOT EXISTS alerts_dst  ON alerts (dst_account);
"""

COPY = ("COPY alerts (txn_id, ts, src_account, dst_account, amount, currency, "
        "payment_format, risk_score, split, is_laundering) FROM STDIN")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="data/HI-Small_Trans.csv")
    ap.add_argument("--model", default=None)
    args = ap.parse_args()

    model_path = args.model or f"artifacts/model-{Path(args.csv).stem}.pt"
    ckpt = torch.load(model_path, weights_only=False)
    data = load(args.csv)
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
        conn.execute(SCHEMA)
        conn.execute("TRUNCATE alerts")
        with conn.cursor().copy(COPY) as copy:
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
