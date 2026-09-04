"""Why a transaction was flagged. Attention weights from the GAT, SHAP on the
baseline. The agent consumes this as evidence -- the difference between a flag
and a defensible flag."""

import numpy as np
import torch

from ml.dataset import Sampler

FANOUT = (15, 10)


def explain_transaction(data, model, node: int, top: int = 5):
    """Risk score for one transaction, plus the neighbouring transactions that
    layer-1 attention weighted most heavily. A self-loop in the result means the
    model is leaning on the transaction's own features rather than its context."""
    nodes, edge_index = Sampler(data, FANOUT).subgraph(np.array([node]))
    model.eval()
    with torch.no_grad():
        logits, att_index, alpha = model.forward_with_attention(data.x[nodes], edge_index)

    local = int(np.searchsorted(nodes, node))
    incoming = att_index[1] == local
    src, weight = att_index[0][incoming], alpha[incoming]
    order = torch.argsort(weight, descending=True)[:top]
    return {
        "risk_score": torch.sigmoid(logits[local]).item(),
        "neighbours": nodes[src[order].numpy()],
        "weights": weight[order].numpy(),
        "is_self": (src[order].numpy() == local),
        "subgraph_size": len(nodes),
    }


def baseline_shap(booster, x, rows: np.ndarray):
    """Per-feature SHAP attributions for the XGBoost baseline."""
    import shap
    values = shap.TreeExplainer(booster).shap_values(x.iloc[rows])
    return dict(zip(x.columns, values.mean(axis=0)))
