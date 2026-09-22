"""Load the transaction table Postgres-side. Node 1 pulls an entity's history
from here rather than holding a 5M-row dataframe in every worker."""

import psycopg

from ml.dataset import load_flat
from ml.score_batch import DSN

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    txn_id         bigint PRIMARY KEY,
    ts             timestamp NOT NULL,
    src_account    text NOT NULL,
    dst_account    text NOT NULL,
    amount         numeric NOT NULL,
    currency       text NOT NULL,
    payment_format text NOT NULL,
    is_laundering  smallint NOT NULL
);
"""
INDEXES = """
CREATE INDEX IF NOT EXISTS txn_src ON transactions (src_account, ts);
CREATE INDEX IF NOT EXISTS txn_dst ON transactions (dst_account, ts);
"""
COPY = ("COPY transactions (txn_id, ts, src_account, dst_account, amount, "
        "currency, payment_format, is_laundering) FROM STDIN")

def main():
    df, _ = load_flat("data/HI-Small_Trans.csv")
    print(f"loading {len(df):,} transactions")

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(SCHEMA)
        conn.execute("TRUNCATE transactions")
        with conn.cursor().copy(COPY) as copy:
            for row in zip(range(len(df)), df["Timestamp"], df["_src"], df["_dst"],
                           df["Amount Paid"], df["Payment Currency"],
                           df["Payment Format"], df["Is Laundering"]):
                copy.write_row((row[0], row[1], row[2], row[3], float(row[4]),
                                row[5], row[6], int(row[7])))
        print("indexing (both endpoints, per plan section 8)")
        conn.execute(INDEXES)
        print("rows:", conn.execute("SELECT count(*) FROM transactions").fetchone()[0])


if __name__ == "__main__":
    main()
