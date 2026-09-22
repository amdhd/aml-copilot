"""Load the seed CSVs into whatever AML_DSN points at.

This is the spin-up path of plan section 9: the container has no CSV to parse
and no model to run, just two small files to COPY, so a fresh database is ready
in seconds instead of the tens of minutes a full load takes.

    python -m scripts.load_seed
"""

import argparse
from pathlib import Path

import psycopg

from ml.score_batch import DSN, SCHEMA as ALERTS_SCHEMA
from scripts.load_transactions import (COPY as TXN_COPY, INDEXES as TXN_INDEXES,
                                       SCHEMA as TXN_SCHEMA)

ALERTS_COPY = ("COPY alerts (txn_id, ts, src_account, dst_account, amount, "
               "currency, payment_format, risk_score, split, is_laundering) "
               "FROM STDIN WITH CSV")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=Path, default=Path("data/seed"))
    args = ap.parse_args()

    with psycopg.connect(DSN, autocommit=True) as conn:
        for name, schema, copy in (
            ("transactions", TXN_SCHEMA, TXN_COPY + " WITH CSV"),
            ("alerts", ALERTS_SCHEMA, ALERTS_COPY),
        ):
            path = args.seed / f"{name}.csv"
            conn.execute(schema)
            conn.execute(f"TRUNCATE {name}")
            with conn.cursor().copy(copy) as dest:
                dest.write(path.read_bytes())
            n = conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            print(f"{name}: {n:,} rows")
        conn.execute(TXN_INDEXES)
        print("indexed")


if __name__ == "__main__":
    main()
