"""Export the reduced demo dataset of plan section 9, preserving txn_ids.

txn_id is a *position* in the graph -- node i is row i of the sorted CSV -- so
the seed cannot renumber anything. It is a row filter and nothing else: the
rows it keeps carry the ids they had in the full dataset, and the GNN still
loads the full graph. Renumbering would change both the neighbourhood the model
sees and the risk scores already published in the README.

What it keeps, and why that is enough: the agent only ever reads transactions
belonging to an alerted transaction's two accounts (gather_context's history
query), so keeping *every* transaction touching those accounts reproduces the
evidence bundle exactly rather than approximately.

    python -m scripts.make_seed --limit 50
"""

import argparse
import csv
import json
from pathlib import Path

import psycopg

from db import DSN

OUT = Path("data/seed")
FIXTURES = Path("eval/fixtures.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=50,
                    help="highest-risk test alerts to keep for the demo queue")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    fixtures = [f["alert_id"] for f in json.loads(FIXTURES.read_text())]

    with psycopg.connect(DSN) as conn:
        queue = [r[0] for r in conn.execute(
            "SELECT txn_id FROM alerts WHERE split = 'test'"
            " ORDER BY risk_score DESC LIMIT %s", (args.limit,)).fetchall()]
        # The eval fixtures are the cases the README's numbers were measured on.
        # A demo database that cannot run them is not the same system.
        missing = [f for f in fixtures if not conn.execute(
            "SELECT 1 FROM alerts WHERE txn_id = %s", (f,)).fetchone()]
        if missing:
            raise SystemExit(f"alerts is missing eval fixtures {missing}; "
                             f"run `make score` against the full CSV first")
        alert_ids = sorted(set(queue) | set(fixtures))

        accounts = [r[0] for r in conn.execute(
            "SELECT src_account FROM alerts WHERE txn_id = ANY(%s)"
            " UNION SELECT dst_account FROM alerts WHERE txn_id = ANY(%s)",
            (alert_ids, alert_ids)).fetchall()]

        args.out.mkdir(parents=True, exist_ok=True)
        counts = {}
        for name, sql, params in (
            ("alerts",
             "SELECT txn_id, ts, src_account, dst_account, amount, currency,"
             " payment_format, risk_score, split, is_laundering FROM alerts"
             " WHERE txn_id = ANY(%s) ORDER BY txn_id", (alert_ids,)),
            ("transactions",
             "SELECT txn_id, ts, src_account, dst_account, amount, currency,"
             " payment_format, is_laundering FROM transactions"
             " WHERE src_account = ANY(%s) OR dst_account = ANY(%s)"
             " ORDER BY txn_id", (accounts, accounts)),
        ):
            path = args.out / f"{name}.csv"
            with conn.cursor() as cur, path.open("w", newline="") as fh:
                writer = csv.writer(fh)
                rows = cur.execute(sql, params).fetchall()
                writer.writerows(rows)
            counts[name] = len(rows)
            print(f"{path}  {len(rows):,} rows")

    print(f"\n{len(alert_ids)} alerts ({args.limit} queue + {len(fixtures)} "
          f"fixtures, overlapping), {len(accounts)} accounts")
    print(f"txn_id range {min(alert_ids):,}..{max(alert_ids):,} -- original "
          f"ids, addressing the full graph")
    return counts


if __name__ == "__main__":
    main()
