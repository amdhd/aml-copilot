"""Illicit-class F1 and AUC-PR. Accuracy is meaningless at this class ratio."""

import numpy as np
from sklearn.metrics import average_precision_score, f1_score, precision_recall_fscore_support


def best_threshold(y, prob):
    grid = np.quantile(prob, np.linspace(0.5, 0.9999, 200))
    return max(grid, key=lambda t: f1_score(y, prob >= t, zero_division=0))


def report(name, y, prob, threshold):
    p, r, f1, _ = precision_recall_fscore_support(
        y, prob >= threshold, average="binary", zero_division=0)
    ap = average_precision_score(y, prob)
    print(f"{name:10s} illicit F1 {f1:.4f}  AUC-PR {ap:.4f}  "
          f"precision {p:.4f}  recall {r:.4f}  (threshold {threshold:.4f})")
    return {"f1": f1, "auc_pr": ap, "precision": p, "recall": r}
