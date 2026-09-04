# AML Investigation Copilot

Graph-neural detection of money laundering, with a deterministic agent workflow for
drafting Suspicious Activity Report narratives that a human approves.

Compliance analysts work a queue of AML alerts. Each investigation means gathering an
entity's transaction context, identifying which laundering typology it matches, checking
it against regulatory guidance, and drafting a narrative. **This system does the
gathering and drafting; the analyst decides.** Nothing is auto-filed.

**Status: weeks 1–2 of 8 complete.** The detection half works end to end — a GAT scores
5.08M transactions into a Postgres alert queue, and every flag is explainable. The agent
workflow, RAG, citation verifier, UI and infrastructure are not built yet. See
[aml-copilot-build-plan.md](aml-copilot-build-plan.md) for the full design and §13 for
findings that changed it.

---

## Results

HI-Small: 5,078,345 transactions, 0.1019% illicit. Temporal split — train 3,554,841
(0.080% illicit), validation 507,834 (0.103%), test 1,015,670 (0.177%). Both models
early-stopped on validation; the decision threshold is chosen on validation and applied
unchanged to test.

| Model | Val AUC-PR | **Test AUC-PR** | Test F1 | Precision | Recall | Threshold | Runtime |
|---|---|---|---|---|---|---|---|
| **GAT** | 0.1465 | **0.3127** | 0.2141 | 0.1287 | 0.6372 | 0.7682 | 2h42m, 34 epochs |
| XGBoost | 0.0908 | 0.1159 | 0.1817 | 0.1452 | 0.2426 | 0.9668 | 46 s |

**AUC-PR is the headline metric, not F1** — see [Why AUC-PR](#why-auc-pr-and-not-f1). On
it the GAT beats the flat baseline **2.7x**, so the graph structure earns its complexity.

The baseline is not a strawman: it gets 18 engineered account aggregates (fan-in/fan-out,
pass-through ratio, velocity) that the GAT is never given. The GAT has to recover that
structure from the graph itself.

### What that means in practice

Of 1,015,670 held-out transactions, 1,797 are genuinely laundering — about 1 in 565.
Picking at random you would hit one 0.18% of the time. Working the model's queue
top-down:

| Alert queue slice | Real laundering |
|---|---|
| Top 100 | 47% |
| Top 500 | **57%** |
| Top 2000 | 42% |

Roughly **300x better than chance** at the head of the queue.

Note the top 500 outscoring the top 100 — that is a known, documented flaw, not a
rounding artifact. See [Known issues](#known-issues).

### Why AUC-PR and not F1

F1 depends on where you put the decision threshold. AUC-PR does not — it measures how
well the model *ranks* suspicious transactions, which is what an alert queue actually
uses.

That distinction is not academic here. Two statistically indistinguishable models
produced test F1 of 0.4009 and 0.2141 — while their AUC-PR was 0.3183 and 0.3127. The
threshold is chosen on 524 validation positives and does not survive the shift to a test
period with a different illicit rate. **Quote F1 only alongside the threshold that
produced it.**

Accuracy and plain AUC-ROC are not reported at all. At 0.1% prevalence a model that
flags nothing scores 99.9% accurate and catches zero criminals.

---

## Quickstart

Requires Python 3.12 (via [uv](https://docs.astral.sh/uv/)), Docker, and ~4 GB disk.

```bash
uv sync
brew install libomp    # xgboost needs the OpenMP runtime on macOS
```

The dataset is not in the repo. Download `HI-Small_Trans.csv` from the Kaggle dataset
`ealtman2019/ibm-transactions-for-anti-money-laundering-aml` into `data/`.

```bash
make train      # GAT   -> artifacts/model-HI-Small_Trans.pt   (~2h45m on an M4)
make baseline   # XGBoost baseline                             (~46s)
make score      # score all 5.08M txns -> Postgres alerts table
```

Postgres 16 with pgvector, needed only for `make score`:

```bash
docker run -d --name aml-pg -e POSTGRES_PASSWORD=aml -e POSTGRES_USER=aml \
  -e POSTGRES_DB=aml -p 5432:5432 pgvector/pgvector:pg16
```

Override the connection with `AML_DSN`. Default is
`postgresql://aml:aml@localhost:5432/aml`.

### Without the dataset

```bash
make smoke      # generates a synthetic CSV in the IBM schema, runs both models
```

This proves the pipeline executes. **Its numbers are not results** — the fixture is 8k
rows with a 5% illicit rate, nothing like the real distribution.

### Watching a run

```bash
make tensorboard                                    # localhost:6006, per-batch loss
uv run python scripts/watch_training.py <logfile>   # localhost:8765, parses the log
```

TensorBoard records from the run that starts after it; the log watcher can attach to a
run already in progress.

---

## How it works

### Transactions as nodes

Transactions are graph **nodes**, not edges. Two transactions are linked if they touch
the same account and are adjacent in time on that account, up to 3 positions apart in
either direction. Every transaction touches two accounts, so a node gets temporal
neighbours on both its sender and its receiver side.

This is what lets the model see laundering as a *pattern* rather than a single odd
payment — a lump arrives and is chopped into pieces pushed out hours later. It stays
`O(n)` in edges rather than exploding quadratically on hub accounts. 5.08M nodes,
52.0M edges.

A same-account transfer touches one account, not two, and enters the timeline once.
Getting this wrong was a real bug — see [Known issues](#known-issues).

### Splits and leakage

Splits are temporal: earliest 70% train, next 10% validation, last 20% test. You predict
the future from the past, as in production.

The baseline's account aggregates are computed **from the training period only**, so a
test-period transaction never sees statistics derived from its own future.

The IBM CSV is not sorted by time. Everything sorts first — taking the first N rows
yields a degenerate split (train covering 11 hours, test covering 9 days).

### Explainability

Every flag carries evidence, because a flag a compliance officer cannot defend is worth
nothing.

- **GAT** — layer-1 attention weights: which neighbouring transactions drove the score.
  A self-loop in the result means the model is leaning on the transaction's own features
  rather than its context.
- **Baseline** — SHAP over the flat features.

`GAT.forward_with_attention` is asserted to return logits identical to `forward`, so the
explanation always describes the computation that actually produced the prediction.

### Alert queue

`make score` scores all 5.08M transactions and writes those above the threshold to
Postgres — 57,019 alerts, indexed on `risk_score` and both account endpoints.

| Split | Alerts | Real laundering | Precision |
|---|---|---|---|
| test | 8,897 | 1,145 | 0.129 |
| val | 1,647 | 195 | 0.118 |
| train | 46,475 | 1,319 | 0.028 |

Alerts originate from the model score and are never hand-picked. Every row records its
split; **the demo queue must filter to `split = 'test'`**, since train-period scores are
in-sample and not honest. Train precision being *lower* than test indicates the model is
not memorising its training period.

---

## Layout

```
ml/
  dataset.py      IBM CSV -> PyG graph; neighbour sampler
  model.py        2-layer GAT, plus the attention call path
  train.py        training loop, early stopping, TensorBoard
  baseline.py     XGBoost on 28 flat features
  explain.py      attention weights + SHAP
  score_batch.py  batch scoring -> Postgres alerts
  metrics.py      illicit-class F1 + AUC-PR
scripts/
  make_smoke_csv.py     synthetic fixture in the IBM schema
  watch_training.py     live view of a run in progress
```

Roughly 500 lines. Weeks 3–8 add `agent/`, `api/`, `rag/`, `ui/`, `eval/` and `infra/`.

---

## Known issues

**The alert queue's head is polluted by self-transfers.** In the test period, 21,441
transactions send from an account to itself and only 5 are laundering — yet 457 of them
sit in the model's top 2,000. This is why the top-500 hit rate (57%) beats the top-100
(47%), which should never happen.

Partly fixed. The original cause was a bug in edge construction: a same-account transfer
entered its account timeline twice under the same key, paired with itself, and those
pairs were dropped as self-loops, leaving the node with half its neighbours. Fixing it
cut self-transfers in the top 2,000 by 22% and raised the top-100 hit rate by 10 points.

The residue is not a code problem. 12.4% of self-transfers are the only transaction on
their account, so they have no neighbours at all and the GAT has no context to work with
— it falls back on raw features, where the enormous amounts dominate. Going further is
feature engineering (give the model its own node degree so it can learn to distrust
isolated nodes), not graph construction.

**Validation has only 524 illicit transactions.** Epoch-to-epoch F1 differences below
roughly 0.03 are noise. This caps how finely anything can be tuned, and it is why the
threshold transfers badly.

**The dataset is synthetic.** Real transaction graphs are messier, labels are far weaker
— most laundering is never labelled at all, because nobody caught it — and the class
imbalance is worse. Real-world performance would be lower.

**Do not compare these numbers to the 0.74–0.86 F1 band** often quoted for GNN-AML work.
That band matches Elliptic (bitcoin) results, where the strong numbers come from tree
ensembles on node features. Published HI-Small work classifies *edges*, not transaction
nodes, using architectural additions this model does not have (port numbering, ego IDs,
reverse message passing).

---

## Implementation notes

**PyG's `NeighborLoader` is unusable on macOS ARM.** It needs `pyg-lib` or
`torch-sparse`, neither of which ships an arm64 wheel for current torch.
`ml.dataset.Sampler` is a ~30-line replacement. Mini-batching is not optional — full-batch
GAT does not fit past ~50k transactions on 16 GB.

**Graph construction is cached.** Building 52M edges over 5M rows takes minutes, so
`load()` writes a `.graph.pt` beside the CSV (1.7 GB; 14s → 0.6s). It is gitignored, and
it is invalidated by deleting it — change the edge rule and you must delete the cache.

**Model artifacts are keyed on the dataset** (`model-HI-Small_Trans.pt`). A smoke run
must never be able to overwrite a real model; it did once, and cost a full retrain.

**Runs are deterministic.** Fixed seeds in torch and numpy; two runs of the same code
reproduce bit-identically.
