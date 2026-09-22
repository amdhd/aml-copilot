"""Node 1: assemble the evidence bundle. No LLM.

Every fact gets an evidence_id. Nothing downstream may assert anything that
does not resolve to a key in this bundle -- that is what makes the citation
verifier in week 5 possible.
"""

import numpy as np
import psycopg
import torch

from ml.dataset import load
from ml.explain import explain_transaction
from ml.model import GAT
from db import DSN

CSV = "data/HI-Small_Trans.csv"
HISTORY_LIMIT = 100
_cache: dict = {}


def _gnn():
    """Graph and model are loaded once per worker, not once per case."""
    if not _cache:
        # Self-generated checkpoint (train.py); not untrusted input.
        ckpt = torch.load("artifacts/model-HI-Small_Trans.pt", weights_only=False)  # nosec B614
        model = GAT(ckpt["in_dim"], ckpt["hidden"])
        model.load_state_dict(ckpt["state_dict"])
        model.eval()
        _cache["data"], _cache["model"] = load(CSV, categories=ckpt["categories"]), model
    return _cache["data"], _cache["model"]


def _check_node(data, txn_id: int, amount: float) -> None:
    """A txn_id is a *position* in the graph, not a key the graph stores.

    Node i is row i of the sorted CSV, and `transactions`/`alerts` carry that
    same row number as their primary key -- three modules agreeing by
    convention, with nothing enforcing it. The seed dataset in section 9 is
    where that convention gets tested: dropping rows renumbers every node after
    the first gap, so node i becomes a different transaction with a different
    neighbourhood, and the attention weights would still look entirely
    plausible. Column 0 of the feature matrix is log1p(amount), so the graph can
    be asked whether it agrees with the database about which transaction this
    is.
    """
    if not 0 <= txn_id < data.num_nodes:
        raise ValueError(
            f"txn_id {txn_id} is outside the {data.num_nodes}-node graph. The "
            f"graph must be built from the full CSV even when the database "
            f"holds only a seed subset -- see section 9.")
    graph_amount = float(np.expm1(data.x[txn_id, 0].item()))
    if not np.isclose(graph_amount, amount, rtol=1e-3):
        raise ValueError(
            f"graph node {txn_id} has amount {graph_amount:,.2f} but the "
            f"database says {amount:,.2f}. The graph and the database were "
            f"built from different row orderings, so txn_id does not address "
            f"the same transaction in both.")


def gather_context(state: dict) -> dict:
    alert_id = state["alert_id"]
    evidence: dict = {}

    with psycopg.connect(DSN) as conn:
        alert = conn.execute(
            "SELECT txn_id, ts, src_account, dst_account, amount, currency, "
            "payment_format, risk_score, split FROM alerts WHERE txn_id = %s",
            (alert_id,)).fetchone()
        if alert is None:
            raise ValueError(f"no alert with txn_id {alert_id}")
        txn_id, ts, src, dst, amount, currency, fmt, risk, split = alert
        evidence[f"alert:{txn_id}"] = {
            "kind": "alert", "txn_id": txn_id, "timestamp": str(ts),
            "src_account": src, "dst_account": dst, "amount": float(amount),
            "currency": currency, "payment_format": fmt,
            "gnn_risk_score": round(float(risk), 4), "split": split,
        }

        # History is prior transactions, most recent first. Ordering by absolute
        # time distance would pull in transactions after the alert too, letting
        # the bundle cite transfers that had not yet occurred.
        #
        # The bound is inclusive: a transfer in the same minute is simultaneous,
        # not future, and several typologies turn on exactly that pairing -- a
        # same-account conversion leg booked alongside its outbound. The alerted
        # transaction is excluded by id instead, since it is already in the
        # bundle as the alert fact. Ties break on txn_id so a LIMIT that cuts
        # through one timestamp is reproducible.
        history = conn.execute(
            "SELECT txn_id, ts, src_account, dst_account, amount, currency, "
            "payment_format FROM transactions "
            "WHERE (src_account IN (%s, %s) OR dst_account IN (%s, %s)) "
            "AND ts <= %s AND txn_id <> %s "
            "ORDER BY ts DESC, txn_id DESC LIMIT %s",
            (src, dst, src, dst, ts, txn_id, HISTORY_LIMIT)).fetchall()

    for h in history:
        # History now spans both endpoints, so direction has to be relative to
        # whichever of the two this transaction actually touches.
        party = src if src in (h[2], h[3]) else dst
        evidence[f"txn:{h[0]}"] = {
            "kind": "transaction", "txn_id": h[0], "timestamp": str(h[1]),
            "src_account": h[2], "dst_account": h[3], "amount": float(h[4]),
            "currency": h[5], "payment_format": h[6], "account": party,
            "direction": "outgoing" if h[2] == party else "incoming",
        }

    data, model = _gnn()
    _check_node(data, int(txn_id), float(amount))
    ex = explain_transaction(data, model, int(txn_id))
    for neighbour, weight, is_self in zip(ex["neighbours"], ex["weights"], ex["is_self"]):
        evidence[f"gnn:{int(neighbour)}"] = {
            "kind": "gnn_attention", "txn_id": int(neighbour),
            "attention_weight": round(float(weight), 4),
            "is_the_alerted_transaction_itself": bool(is_self),
        }
    evidence[f"gnn:subgraph:{txn_id}"] = {
        "kind": "gnn_subgraph", "txn_id": int(txn_id),
        "risk_score": round(ex["risk_score"], 4),
        "neighbourhood_size": ex["subgraph_size"],
    }

    return {"evidence": evidence}
