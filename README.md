# AML Investigation Copilot

![AWS architecture: ALB, api and worker tasks on ECS Fargate, RDS Postgres with pgvector, S3, ECR](infra/architecture.png)

Graph-neural detection of money laundering, with a deterministic agent workflow for
drafting Suspicious Activity Report narratives that a human approves.

Compliance analysts work a queue of AML alerts. Each investigation means gathering an
entity's transaction context, identifying which laundering typology it matches, checking
it against regulatory guidance, and drafting a narrative. **This system does the
gathering and drafting; the analyst decides.** Nothing is auto-filed.

**Status: all 8 weeks built; the analyst-time comparison is not yet recorded.** A GAT scores 5.08M transactions into a Postgres
alert queue; a deterministic LangGraph workflow gathers evidence, classifies the
laundering typology, retrieves regulatory guidance, drafts a cited SAR narrative,
verifies every citation in plain Python, and parks the case at a human gate for an
analyst to approve or reject in the UI. It deploys to ECS Fargate with Terraform — see
[Deploy](#deploy-aws). See [aml-copilot-build-plan.md](aml-copilot-build-plan.md) for
the design and §13 for findings that changed it.

### The UI

The alert queue, highest GNN risk score first, test split only:

![Alert queue: 50 alerts ranked by GNN risk score, with sender, receiver, amount and same-account transfers labelled](docs/screenshots/alert-queue.png)

A case parked at the human gate. The suggested typology carries a warning not to triage
on it; every narrative sentence carries the evidence ids it rests on, including the red
flag it matches (hover one to resolve it against the bundle); below is the GNN
neighbourhood the model attended to:

![Case view: suggested typology with a calibration warning, a verified SAR narrative with per-sentence citations, and the GNN subgraph](docs/screenshots/case-view.png)

## Agent eval results

`make eval` — 8 fixtures, labelled by inspecting each account's transaction pattern
rather than by copying the dataset's per-transaction `Is Laundering` column (two of the
eight disagree with it). Provider `deepseek-flash` on DeepSeek, run 2026-09-30.

| Metric | Target | Result |
|---|---|---|
| Typology accuracy | report | **4/8 (50%)** |
| Citation validity | **100%** | **100.0% (155/155)** |
| Hallucinated entities | **0** | **0** |
| Narratives citing a red flag | report | **7/7** (11 cites) |
| Guidance-only sentences | **0** | **0** |
| Model-only sentences | **0** | **0** |
| Unsupported accounts, amounts, dates | **0** | **0** (of 182 checked) |
| Escalated after retry | report | 0 |
| Provider errors | report | **0** |
| p50 / p95 latency | report | 53.1s / 62.4s |
| Tokens per case | report | 7,609 in, 3,911 out |
| Prompt cache hit rate | report | 77.0% |

**Read these two numbers together.** Deterministic verification works: across 155
citations in 7 narratives, every `evidence_id` in every drafted sentence resolved to a
fact the pipeline actually assembled, and every account, amount and date a sentence
named — 78, 43 and 61 of them across 56 sentences — appears in a fact that same
sentence cites. No draft needed its retry. But the same run classified the typology
correctly only four times in eight. **A perfectly cited narrative about the wrong
typology is a perfectly cited wrong answer.** The verifier proves the narrative rests
on real evidence; it proves nothing about whether the conclusion is right.

### Red flags, cited beside the evidence

Narratives now cite the regulatory red flag a pattern matches — FATF, FFIEC Appendix
F/G, or FinCEN — alongside the transactions that show it. Before this run they cited
guidance 0 times in 6. A sentence whose *only* citations are guidance fails
verification: every id in it would resolve, while nothing from the case supported it.

Each cited chunk was read against its sentence and contains the pattern the sentence
states. Three things that are true of these citations and belong with the count:

- **Citations make a wrong answer look better grounded, not more right.** Fixture
  4385373 is labelled `none`; the classifier said `layering`, and two sentences of
  its narrative cite layering red flags, one from FinCEN and one from FFIEC. The
  verifier checks that the red flag exists and sits beside real transactions. It
  cannot check that the red flag applies.
- **Rapid movement leans on a virtual-asset document.** Its closest match is FATF's
  *Virtual Assets* indicator for "multiple high-value transactions in short
  succession", cited in all three rapid-movement narratives for fiat transfers. The
  wording fits; the document's scope does not.
- **Most cites go to one FinCEN chunk.** 6 of 11 cite the same list of common
  patterns in FinCEN's SAR narrative guidance (layering across multiple accounts,
  unusual mixed deposits, bursts of activity in a short period). It is a genuine
  red-flag list inside a report-writing guide, not writing advice.

The cache hit rate is the one design decision that measured cleanly: §5 builds every
prompt `[system][case data]` with no interleaving, and the stable prefix hits. It moves
run to run with how much evidence each case carries — 58.0%, 74.2% and 75.8% on earlier
runs, 77.0% here — so treat it as a working mechanism rather than a fixed figure.

Caveats that belong with the table:

- **The typology number is not reproducible to ±1.** The provider is not deterministic
  at `temperature=0` — one fixture returned three different typologies across three
  samples of identical input, and between runs fixtures swap one wrong answer for
  another without any change in their evidence. 4/8 is a single draw, not a
  measurement. Quote it with a range across runs or not at all.
- **Confidence is not calibrated.** On the previous run it was inverted — every answer
  above 0.65 was wrong. On this one it is inverted again: the most confident answer
  (0.89) is wrong, and every right answer scores between 0.50 and 0.68. Its ordering
  does not hold from one run to the next.
  Confidence cannot be used for triage, and the UI labels it as such.
- **One fixture produced no narrative,** by classifying `none` and short-circuiting
  before drafting. 155/155 covers the seven cases that drafted.
- **Latency is the reasoning model, not throttling.** `deepseek-flash` spends thousands
  of tokens on chain-of-thought per call, which is why output tokens exceed an earlier
  Groq run several times over.

The per-node token budgets (8,000) are sized for a model that bills chain-of-thought
against `max_tokens`. They exceed Groq's free-tier output cap — read §13 before
switching providers.

Evidence history is restricted to transactions at or before the alert. §13 records why,
and the open question that choice raises.

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

### The UI

Four screens: the alert queue, a case with every citation resolvable on hover,
the GNN subgraph for that case, and the approve/reject gate. No auth — single
tenant, demo only.

```bash
npm --prefix ui install
npm --prefix ui run dev     # localhost:5173, proxies /api to localhost:8000
```

It needs `make api` and `make worker` running, and Postgres and Redis up.
A case parked at the gate is reachable by link (`/?case=<case_id>`), because
that wait is measured in days.

`npm --prefix ui run build` instead, and `make api` serves the built UI itself at
`localhost:8000` — the way it runs deployed, with the API under `/api`.

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

## Deploy (AWS)

Runs on ECS Fargate in `ap-southeast-1` (diagram at the top). **This is not a 24/7
service** — it is applied before a demo and destroyed after. Two Terraform layers with
separate state:

| Layer | Contains | Lifecycle | Approx. cost |
|---|---|---|---|
| `infra/persistent` | VPC, subnets, security groups, ECR, S3 (graph cache), log group, SSM key, IAM, $20 budget alarm | applied once | ~$0.20/mo |
| `infra/ephemeral` | RDS Postgres 16 (`db.t4g.micro`), ALB, api task + Redis sidecar, worker on Fargate Spot, seed task | per demo | ~$0.11/hr |

Estimates at on-demand `ap-southeast-1` prices when this was built; the budget alarm is
the real guard.

**Once:**

```bash
cp infra/persistent/terraform.tfvars.example infra/persistent/terraform.tfvars   # email, your IP
cd infra/persistent && terraform init && terraform apply
aws ssm put-parameter --name /aml-copilot/llm-api-key --type SecureString \
  --overwrite --value "$(grep '^AML_LLM_API_KEY=' ../../.env | cut -d= -f2-)"
```

Then upload the graph cache the worker fetches on start-up:

```bash
aws s3 cp data/HI-Small_Trans.None.graph.pt s3://<artifacts_bucket>/graph/<sha256[:16]>/HI-Small_Trans.None.graph.pt
```

**Per demo** (~6 minutes, most of it RDS). The image is tagged with the current git
commit, so push once per commit you want to run:

```bash
make push        # build and push this commit's image (refuses with uncommitted changes)
make demo-up     # apply, seed RDS, wait for the API and worker, print the URL
make demo-down   # destroy, then check no RDS, ALB, ECS cluster or elastic IP remains
```

`demo-up` and `demo-down` each ask before touching billed resources.

Choices worth defending:

- **No NAT Gateway** (~$32/mo idle). The embedding model is baked into the image, so
  the only egress is ECR, S3 and the LLM API; tasks reach them through public IPs and
  security groups admit only the ALB. Production would put tasks in private subnets
  behind VPC endpoints.
- **The database is rebuilt every demo and seeded in under a second.** The seed keeps
  every transaction touching an alerted account, so evidence bundles are reproduced
  exactly; the graph itself is never subsetted (§13, "txn_id is a graph position").
- **Redis is a sidecar in the on-demand api task**, not ElastiCache: cheaper, and a
  Spot reclaim of the worker cannot take the queue with it. Run state lives in the
  Postgres checkpointer either way.
- **The ALB is open to `allowed_cidrs` only.** There is no auth, and every case spends
  LLM credit.
- **Secrets stay out of Terraform state and task definitions.** The LLM key is set from
  the CLI; the DSN reaches containers through SSM.

Verified end to end on 2026-09-23: a case through the public ALB parked at the human
gate after 31s and resumed to `approved` from the RDS checkpoint.

**Data residency.** A bank could not send transaction data to a third-party LLM API.
The production path is the same class of model self-hosted inside the VPC; nothing else
in the architecture changes.

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
agent/
  graph.py        LangGraph, fixed edges, Postgres checkpointer
  nodes/          gather_context, classify_typology, retrieve_guidance,
                  draft_narrative, human_review
  verify.py       citation verifier, plain Python
  schemas.py      one pydantic schema per LLM node
api/
  main.py         FastAPI; serves the built UI in deployment
  worker.py       arq worker that runs the graph
rag/ingest.py     red-flag corpus -> pgvector, local embeddings
ui/               React: queue, case, subgraph, approve/reject
eval/             8 labelled fixtures, make eval
infra/
  persistent/     VPC, ECR, S3, IAM, budget -- applied once
  ephemeral/      ECS, ALB, RDS -- apply/destroy per demo
  diagram.py      renders architecture.png
scripts/
  make_seed.py / load_seed.py   reduced demo dataset, preserving txn_ids
  make_smoke_csv.py             synthetic fixture in the IBM schema
  watch_training.py             live view of a run in progress
config.py, db.py  config and table DDL, importable without the ML stack
Dockerfile        one image for api, worker and seed
```

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

**The corpus is four documents, added by hand.** FATF and FFIEC publish behind bot
protection, so the PDFs are downloaded manually into `data/corpus/` (gitignored) and
only FFIEC pages 345–356 (Appendices F and G) are ingested. The seed carries the
ingested chunks, so a clone deploys with them, but re-running ingest needs the PDFs.

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
