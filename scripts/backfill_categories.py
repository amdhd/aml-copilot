"""One-off: record the one-hot vocabulary into checkpoints trained before it
was saved.

model-HI-Small_Trans.pt is 2h42m of training and the vocabulary is derivable
from the CSV it was trained on, so there is no reason to retrain for this. The
derivation is only trustworthy if it reproduces the features the weights were
actually fit to -- so this rebuilds them and refuses to write unless they come
back bit-identical to the cached graph.

    python -m scripts.backfill_categories --csv data/HI-Small_Trans.csv
"""

import argparse
from pathlib import Path

import pandas as pd
import torch

from ml.dataset import _features


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="data/HI-Small_Trans.csv")
    args = ap.parse_args()

    ckpt_path = Path("artifacts") / f"model-{Path(args.csv).stem}.pt"
    cache = Path(args.csv).with_suffix(".None.graph.pt")
    ckpt = torch.load(ckpt_path, weights_only=False)  # nosec B614
    if "categories" in ckpt:
        print(f"{ckpt_path.name} already carries its vocabulary; nothing to do")
        return

    # The same preparation load() does, because the vocabulary is a property of
    # that exact dataframe.
    print(f"reading {args.csv}")
    df = pd.read_csv(args.csv)
    df["Timestamp"] = pd.to_datetime(df["Timestamp"], format="%Y/%m/%d %H:%M")
    df = df.sort_values("Timestamp", kind="stable").reset_index(drop=True)
    sender = df["From Bank"].astype(str) + "-" + df["Account"].astype(str)
    receiver = df["To Bank"].astype(str) + "-" + df["Account.1"].astype(str)
    codes, _ = pd.factorize(pd.concat([sender, receiver], ignore_index=True))
    df["_src"], df["_dst"] = codes[: len(df)], codes[len(df):]

    x, categories = _features(df)
    print(f"derived {len(categories)} categories, in_dim {x.shape[1]}")
    if x.shape[1] != ckpt["in_dim"]:
        raise SystemExit(
            f"derived in_dim {x.shape[1]} != checkpoint in_dim {ckpt['in_dim']}")

    # The proof: the features this vocabulary produces are the ones the model
    # was trained on, not merely the same width.
    if not cache.exists():
        raise SystemExit(f"{cache} is missing; cannot verify against training features")
    cached = torch.load(cache, weights_only=False)  # nosec B614
    if not torch.equal(cached.x, x):
        raise SystemExit("rebuilt features differ from the cached graph -- "
                         "the derived vocabulary is not the one that trained this model")
    print("rebuilt features are bit-identical to the cached graph")

    ckpt["categories"] = categories
    torch.save(ckpt, ckpt_path)
    print(f"wrote vocabulary into {ckpt_path.name}")

    # Patch the cache too: load() now rebuilds any graph whose vocabulary it
    # cannot confirm, and rebuilding this one costs minutes.
    if getattr(cached, "categories", None) != categories:
        cached.categories = categories
        torch.save(cached, cache)
        print(f"wrote vocabulary into {cache.name}")


if __name__ == "__main__":
    main()
