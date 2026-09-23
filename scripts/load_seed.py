"""Load the seed CSVs into whatever AML_DSN points at.

This is the spin-up path of plan section 9: the container has no CSV to parse
and no model to run, just three small files to COPY, so a fresh database is ready
in seconds instead of the tens of minutes a full load takes.

    python -m scripts.load_seed
"""

import argparse
from pathlib import Path

import psycopg

from db import (ALERTS_COPY, ALERTS_SCHEMA, DSN, GUIDANCE_COPY,
                GUIDANCE_INDEX, GUIDANCE_SCHEMA, TXN_COPY, TXN_INDEXES,
                TXN_SCHEMA)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=Path, default=Path("data/seed"))
    args = ap.parse_args()

    with psycopg.connect(DSN, autocommit=True) as conn:
        for name, schema, copy in (
            ("transactions", TXN_SCHEMA, TXN_COPY + " WITH CSV"),
            ("alerts", ALERTS_SCHEMA, ALERTS_COPY + " WITH CSV"),
            ("guidance", GUIDANCE_SCHEMA, GUIDANCE_COPY + " WITH CSV"),
        ):
            path = args.seed / f"{name}.csv"
            conn.execute(schema)
            conn.execute(f"TRUNCATE {name}")
            with conn.cursor().copy(copy) as dest:
                dest.write(path.read_bytes())
            n = conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            print(f"{name}: {n:,} rows")
        conn.execute(TXN_INDEXES)
        conn.execute(GUIDANCE_INDEX)
        print("indexed")


if __name__ == "__main__":
    main()
