"""Load the transaction table Postgres-side. Node 1 pulls an entity's history
from here rather than holding a 5M-row dataframe in every worker."""

import psycopg

from db import DSN, TXN_COPY, TXN_INDEXES, TXN_SCHEMA
from ml.dataset import load_flat

def main():
    df, _ = load_flat("data/HI-Small_Trans.csv")
    print(f"loading {len(df):,} transactions")

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(TXN_SCHEMA)
        conn.execute("TRUNCATE transactions")
        with conn.cursor().copy(TXN_COPY) as copy:
            for row in zip(range(len(df)), df["Timestamp"], df["_src"], df["_dst"],
                           df["Amount Paid"], df["Payment Currency"],
                           df["Payment Format"], df["Is Laundering"]):
                copy.write_row((row[0], row[1], row[2], row[3], float(row[4]),
                                row[5], row[6], int(row[7])))
        print("indexing (both endpoints, per plan section 8)")
        conn.execute(TXN_INDEXES)
        print("rows:", conn.execute("SELECT count(*) FROM transactions").fetchone()[0])


if __name__ == "__main__":
    main()
