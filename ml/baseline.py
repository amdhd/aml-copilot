"""XGBoost on flat engineered features. If this beats the GAT, the graph
structure is not earning its complexity — report it either way.

Account aggregates are computed from the training period only, so a test-period
transaction never sees statistics derived from its own future.
"""

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb

from ml.dataset import load_flat
from ml.metrics import best_threshold, report

AGG = ["n_in", "n_out", "sum_in", "sum_out", "mean_in", "mean_out",
       "fan_ratio", "passthrough", "velocity"]


def _account_features(train: pd.DataFrame) -> pd.DataFrame:
    out = train.groupby("_src").agg(n_out=("Amount Paid", "size"),
                                    sum_out=("Amount Paid", "sum"))
    inc = train.groupby("_dst").agg(n_in=("Amount Received", "size"),
                                    sum_in=("Amount Received", "sum"))
    a = out.join(inc, how="outer").fillna(0.0)
    days = max((train["Timestamp"].max() - train["Timestamp"].min()).days, 1)
    a["mean_out"] = a.sum_out / a.n_out.clip(lower=1)
    a["mean_in"] = a.sum_in / a.n_in.clip(lower=1)
    a["fan_ratio"] = a.n_in / (a.n_out + 1)
    a["passthrough"] = (np.minimum(a.sum_in, a.sum_out)
                        / (np.maximum(a.sum_in, a.sum_out) + 1))
    a["velocity"] = (a.n_in + a.n_out) / days
    return a[AGG]


def _matrix(df: pd.DataFrame, accounts: pd.DataFrame) -> pd.DataFrame:
    ts = df["Timestamp"]
    x = pd.DataFrame({
        "log_paid": np.log1p(df["Amount Paid"]),
        "log_recv": np.log1p(df["Amount Received"]),
        "log_ratio": np.log1p(df["Amount Received"]) - np.log1p(df["Amount Paid"]),
        "same_currency": (df["Payment Currency"] == df["Receiving Currency"]).astype(int),
        "same_bank": (df["From Bank"] == df["To Bank"]).astype(int),
        "self_txn": (df["_src"] == df["_dst"]).astype(int),
        "hour": ts.dt.hour,
        "dayofweek": ts.dt.dayofweek,
        "format": pd.factorize(df["Payment Format"])[0],
        "currency": pd.factorize(df["Payment Currency"])[0],
    })
    for side, key in (("s", "_src"), ("d", "_dst")):
        joined = accounts.reindex(df[key]).reset_index(drop=True)
        x[[f"{c}_{side}" for c in AGG]] = joined  # unseen accounts -> NaN
    return x


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="data/HI-Small_Trans.csv")
    ap.add_argument("--max-rows", type=int, default=None)
    args = ap.parse_args()

    df, split = load_flat(args.csv, args.max_rows)
    y = df["Is Laundering"].to_numpy()
    x = _matrix(df, _account_features(df[split == 0]))
    print(f"{len(df)} txns, {x.shape[1]} features, {y.mean():.4%} illicit")

    tr, va, te = split == 0, split == 1, split == 2
    model = xgb.XGBClassifier(
        n_estimators=600, max_depth=6, learning_rate=0.1,
        subsample=0.8, colsample_bytree=0.8,
        scale_pos_weight=(y[tr] == 0).sum() / (y[tr] == 1).sum(),
        eval_metric="aucpr", early_stopping_rounds=30, n_jobs=-1,
    )
    model.fit(x[tr], y[tr], eval_set=[(x[va], y[va])], verbose=False)

    Path("artifacts").mkdir(exist_ok=True)
    model.save_model("artifacts/baseline.json")

    p_val = model.predict_proba(x[va])[:, 1]
    threshold = best_threshold(y[va], p_val)
    report("XGB val", y[va], p_val, threshold)
    report("XGB test", y[te], model.predict_proba(x[te])[:, 1], threshold)


if __name__ == "__main__":
    main()
