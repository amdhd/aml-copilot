"""Connection string and table definitions, importable without the ML stack.

These lived in ml/score_batch.py and scripts/load_transactions.py, which are
offline entry points that import torch, pandas and scikit-learn. api/ needed
the DSN and nothing else from ml/, so `from ml.score_batch import DSN` cost the
API process 501MB resident and 3.1s of start-up to obtain one string -- and put
torch into an image that never runs it. The seed loader paid the same toll for
a CREATE TABLE.

Nothing here imports anything but os, and that is the point. `cases` is not
here: api/main.py is its only user and owns its migration.
"""

import os

DSN = os.environ.get("AML_DSN", "postgresql://aml:aml@localhost:5432/aml")

ALERTS_SCHEMA = """
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

ALERTS_COPY = ("COPY alerts (txn_id, ts, src_account, dst_account, amount, "
               "currency, payment_format, risk_score, split, is_laundering) "
               "FROM STDIN")

TXN_SCHEMA = """
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

# Both endpoints, per plan section 8: node 1's history query filters on
# src_account OR dst_account, so one index on either side alone leaves half the
# query scanning.
TXN_INDEXES = """
CREATE INDEX IF NOT EXISTS txn_src ON transactions (src_account, ts);
CREATE INDEX IF NOT EXISTS txn_dst ON transactions (dst_account, ts);
"""

TXN_COPY = ("COPY transactions (txn_id, ts, src_account, dst_account, amount, "
            "currency, payment_format, is_laundering) FROM STDIN")
