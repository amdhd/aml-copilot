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
from ml.score_batch import DSN

CSV = "data/HI-Small_Trans.csv"
HISTORY_LIMIT = 100
_cache: dict = {}


def _gnn():
    """Graph and model are loaded once per worker, not once per case."""
    if not _cache:
        ckpt = torch.load("artifacts/model-HI-Small_Trans.pt", weights_only=False)
        model = GAT(ckpt["in_dim"], ckpt["hidden"])
        model.load_state_dict(ckpt["state_dict"])
        model.eval()
        _cache["data"], _cache["model"] = load(CSV), model
    return _cache["data"], _cache["model"]


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

        history = conn.execute(
            "SELECT txn_id, ts, src_account, dst_account, amount, currency, "
            "payment_format FROM transactions "
            "WHERE src_account IN (%s, %s) OR dst_account IN (%s, %s) "
            "ORDER BY abs(extract(epoch FROM ts - %s)) LIMIT %s",
            (src, dst, src, dst, ts, HISTORY_LIMIT)).fetchall()

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

    return {"evidence": evidence, "entity": src}
