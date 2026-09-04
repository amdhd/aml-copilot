"""Train the GAT and report illicit-class F1 / AUC-PR on a temporal test split."""

import argparse
import time
from pathlib import Path

import numpy as np
import torch
from torch import nn
from torch.utils.tensorboard import SummaryWriter

from ml.dataset import Sampler, load
from ml.metrics import best_threshold, report
from ml.model import GAT

FANOUT = (15, 10)
PATIENCE = 8


def evaluate(model, sampler, nodes, batch=8192):
    model.eval()
    probs, ys = [], []
    with torch.no_grad():
        for i in range(0, len(nodes), batch):
            x, edge_index, pos, y = sampler.batch(nodes[i:i + batch])
            probs.append(torch.sigmoid(model(x, edge_index)[pos]))
            ys.append(y)
    return torch.cat(ys).numpy(), torch.cat(probs).numpy()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="data/HI-Small_Trans.csv")
    ap.add_argument("--max-rows", type=int, default=None)
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--batch-size", type=int, default=1024)
    ap.add_argument("--hidden", type=int, default=64)
    ap.add_argument("--lr", type=float, default=3e-3)
    args = ap.parse_args()

    torch.manual_seed(0)
    t0 = time.time()
    writer = SummaryWriter(f"runs/{time.strftime('%m%d-%H%M')}")
    data = load(args.csv, args.max_rows)
    train_nodes = np.flatnonzero(data.train_mask.numpy())
    val_nodes = np.flatnonzero(data.val_mask.numpy())
    test_nodes = np.flatnonzero(data.test_mask.numpy())
    print(f"{data.num_nodes} txns, {data.num_edges} edges, "
          f"{data.y.float().mean():.4%} illicit, loaded in {time.time() - t0:.1f}s")

    train_y = data.y[data.train_mask]
    # Raw neg/pos is ~1244. sqrt damping was tried and lost on validation
    # (0.136 vs 0.209 F1); at 0.1% prevalence the strong weight earns its keep.
    pos_weight = ((train_y == 0).sum() / (train_y == 1).sum()).float()
    print(f"pos_weight {pos_weight:.1f}")
    loss_fn = nn.BCEWithLogitsLoss(pos_weight=pos_weight)
    model = GAT(data.num_features, args.hidden)
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr, weight_decay=5e-4)
    # XGBoost already trains to early stopping on validation; the GAT gets the
    # same protocol so the comparison is not decided by the epoch cap.
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        optimizer, mode="max", factor=0.5, patience=3)

    train_sampler = Sampler(data, FANOUT, seed=0)
    eval_sampler = Sampler(data, FANOUT)
    rng = np.random.default_rng(0)
    step = 0
    best_f1, best_state, best_epoch, best_val = -1.0, None, 0, None

    for epoch in range(1, args.epochs + 1):
        model.train()
        order = rng.permutation(train_nodes)
        total = 0.0
        for i in range(0, len(order), args.batch_size):
            x, edge_index, pos, y = train_sampler.batch(order[i:i + args.batch_size])
            optimizer.zero_grad()
            loss = loss_fn(model(x, edge_index)[pos], y.float())
            loss.backward()
            optimizer.step()
            total += loss.item()
            writer.add_scalar("train/loss", loss.item(), step)
            step += 1

        y_val, p_val = evaluate(model, eval_sampler, val_nodes)
        threshold = best_threshold(y_val, p_val)
        metrics = report(f"epoch {epoch:2d} val", y_val, p_val, threshold)
        for name, value in metrics.items():
            writer.add_scalar(f"val/{name}", value, epoch)
        writer.add_scalar("train/lr", optimizer.param_groups[0]["lr"], epoch)
        scheduler.step(metrics["f1"])
        if metrics["f1"] > best_f1:
            best_f1, best_state, best_threshold_ = metrics["f1"], model.state_dict(), threshold
            best_epoch, best_val = epoch, metrics
        elif epoch - best_epoch >= PATIENCE:
            print(f"no validation gain for {PATIENCE} epochs, stopping")
            break

    model.load_state_dict(best_state)
    y_test, p_test = evaluate(model, eval_sampler, test_nodes)
    print(f"\nbest epoch {best_epoch}")
    print(f"GAT val    illicit F1 {best_val['f1']:.4f}  AUC-PR {best_val['auc_pr']:.4f}  "
          f"precision {best_val['precision']:.4f}  recall {best_val['recall']:.4f}")
    report("GAT test", y_test, p_test, best_threshold_)
    print(f"total {time.time() - t0:.1f}s")

    writer.close()
    Path("artifacts").mkdir(exist_ok=True)
    # Keyed on the dataset: a smoke run must never overwrite a real model.
    out = Path("artifacts") / f"model-{Path(args.csv).stem}.pt"
    torch.save({"state_dict": best_state, "in_dim": data.num_features,
                "hidden": args.hidden, "threshold": float(best_threshold_)}, out)
    print(f"saved {out}")


if __name__ == "__main__":
    main()
