# AML Investigation Copilot — Build Plan

GNN detection (PyTorch) + deterministic agent workflow (LangGraph) in one Python repo.

## Design rules

- No features beyond what is specified here.
- No abstractions for single-use code.
- No configurability that was not requested.
- No error handling for impossible scenarios.
- If a file hits 200 lines and could be 50, rewrite it.

---

## 1. What this is

Compliance analysts work a queue of AML alerts. Each investigation means: gather the
entity's transaction context, figure out which laundering typology it matches, check it
against regulatory guidance, and draft a Suspicious Activity Report narrative. The
analyst approves or rejects. This system does the gathering and drafting; the human
decides.

**Scale target: 20–50 concurrent investigations.** Not 1000 users. A large bank has
~50–200 analysts total. Designing for consumer-scale concurrency here is
overengineering and reads as not understanding the user.

**Reliability target:** never lose case state across the human approval gate, and never
emit a narrative sentence that isn't traceable to real evidence.

---

## 2. Architecture

```
OFFLINE (local machine or spot EC2 — has GPU)
  IBM AML CSV → PyG dataset → train GAT → model.pt
                                  ↓
                         batch score all transactions
                                  ↓
                            alerts table (Postgres)

RUNTIME (ECS Fargate — CPU only)
  React UI ──HTTP──▶ FastAPI ──enqueue──▶ Redis ──▶ arq worker
       ▲                 │                              │
       │                 │                       LangGraph run
       │                 │                              │
       │                 │              ┌───────────────┼───────────────┐
       │                 │              │               │               │
       │                 │         GNN tool        pgvector RAG    DeepSeek API
       │                 │         (in-process)     (guidance)   (deepseek-v4-flash)
       │                 ▼                              ▼
       └─────────── RDS Postgres: app data + transactions + checkpoints + embeddings
```

Two ECS services, one image: `api` and `worker`. Model artifact baked into the image.

### Why async is non-negotiable

A run takes 10–20s, and pauses indefinitely at the human gate. A blocking HTTP request
cannot represent that. API returns a `case_id` immediately; UI polls for status.

### Why Fargate shapes the ML side

**Fargate has no GPU.** Train offline, commit `model.pt`, run CPU-only inference. A GAT
forward pass on a k-hop subgraph is milliseconds on CPU — this is not the bottleneck.
Do not design for in-cluster retraining.

---

## 3. Stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.12, `uv` | One language, both halves |
| API | FastAPI | Already known from prior work |
| Queue | Redis + `arq` | Async-native; Celery is heavier than this needs |
| DB | Postgres 16 + pgvector | App data, transactions, checkpoints, embeddings — one system |
| ML | PyTorch + PyG | GAT on transaction graph |
| Agent | LangGraph, Postgres checkpointer | Durable state across human gate |
| LLM | DeepSeek `deepseek-v4-flash` | Cheap; OpenAI-compatible; 1M context |
| UI | React + Vite, `react-force-graph` | Subgraph render for one case |
| Infra | Terraform → ECS Fargate, RDS, ElastiCache | Existing Terraform strength |

### Explicitly NOT used

Neo4j · Kafka · TorchServe/vLLM · microservices · feature store · auth system ·
Celery · Kubernetes · a separate vector DB.

Rationale for the two most tempting: **Neo4j** adds a system to learn and operate for a
dataset that loads fine into PyG from parquet. **A model-serving service** adds a network
hop and a deployment target to serve a model whose inference is sub-millisecond.

---

## 4. The GNN

**Dataset:** IBM Transactions for AML (Kaggle). Synthetic but the industry-standard
benchmark for this problem.

**Model:** Graph Attention Network, PyG. Represent **transactions as nodes**, not edges —
published comparisons find this outperforms edge-representation under heavy class
imbalance.

**Metrics:** F1 on the *illicit* class and AUC-PR. Not accuracy, not plain AUC-ROC —
illicit transactions are a tiny minority and accuracy is meaningless here.

~~Published GNN-AML work lands around 0.74–0.86 F1 on illicit; that's your comparison
band.~~ **Corrected 2026-09-04 — that band is wrong for this task.** It matches published
Elliptic (bitcoin) illicit-class results, where the strong numbers come from tree
ensembles on node features. Published HI-Small work classifies *edges*, not transaction
nodes, and reaches its numbers with architectural additions this build does not have
(port numbering, ego IDs, reverse message passing). Do not quote 0.74–0.86 in a README
or an interview. **The comparison that matters is the internal one: GAT vs the XGBoost
baseline on an identical split.**

**Baseline:** XGBoost on flat engineered features (fan-in/fan-out ratios, velocity,
pass-through ratio). The GNN must beat it, or the graph structure isn't earning its
complexity. Report both. If XGBoost wins, that is a real and reportable finding — do not
bury it.

**Measured 2026-09-04 on full HI-Small** (5,078,345 txns, 0.1019% illicit; temporal split
70/10/20; threshold picked on validation; both models early-stopped on validation):

| Model | Val F1 | Val AUC-PR | Test F1 | Test AUC-PR | Precision | Recall | Runtime |
|---|---|---|---|---|---|---|---|
| **GAT** | 0.2237 | 0.1484 | **0.4009** | **0.3183** | 0.3039 | 0.5888 | 97 min |
| XGBoost | 0.1502 | 0.0908 | 0.1817 | 0.1159 | 0.1452 | 0.2426 | 46 s |

GAT wins 2.2x on illicit F1, 2.7x on AUC-PR. **Gate passed — the graph earns its
complexity.** The shape of the win is recall: 0.59 vs 0.24 at roughly double the
precision. For a comparable alert volume (3,481 vs 3,003 alerts) the GAT catches 1,058
of 1,797 real cases against the baseline's 436.

Test F1 runs well above validation F1 for both models because the illicit rate rises
through the file (0.080% train, 0.103% val, 0.177% test). **Validation is the honest
held-out estimate; test is inflated by the file's ordering.** Cannot be fixed — the
split is temporal by design. Report both columns.

**Explainability:** attention weights from the GAT + SHAP on the baseline features. The
agent consumes this as evidence. This is the difference between a flag and a defensible
flag.

**Two call paths, one artifact:**
1. Batch scoring → populates the alert queue. Alerts must originate from the model, not
   be hand-picked, or the demo is fake.
2. `get_entity_risk_context(entity_id)` → k-hop subgraph + risk score + top attention
   contributions. Called by the agent mid-investigation.

---

## 5. The LangGraph workflow

**Deterministic graph with fixed edges. Not a ReAct loop.**

Two reasons, and the second is the one that matters: DeepSeek's native tool-calling is
less reliable for multi-step autonomous loops than Claude or GPT — and, more importantly,
a regulator-facing process needs to be reproducible. An agent that improvises a different
investigation path each run is not auditable. This is a compliance design decision that
happens to also work around a model limitation.

| # | Node | LLM? | Output |
|---|---|---|---|
| 1 | `gather_context` | No | Alert, entity, txn history, GNN subgraph + explanation. Each fact gets an `evidence_id`. |
| 2 | `classify_typology` | Yes | Enum: `structuring \| layering \| smurfing \| trade_based \| rapid_movement \| none` + confidence. JSON schema. |
| 3 | `retrieve_guidance` | No | pgvector query over FATF/FinCEN corpus, keyed on typology + entity facts. |
| 4 | `draft_narrative` | Yes | `[{text, evidence_ids[]}]` — sentence-level, each carrying its sources. JSON schema. |
| 5 | `verify_citations` | No | Deterministic Python. Every `evidence_id` must resolve to real context. Fail → one retry → escalate flagged. |
| 6 | `human_review` | No | LangGraph `interrupt`. State persisted to Postgres. Waits. |

**Two LLM calls per case.** Everything else is code. This keeps latency at 10–20s, cost
near zero, and the eval harness tractable.

### The citation verifier is the centerpiece

`verify_citations` is plain Python — it checks that each `evidence_id` in the draft
resolves to something that actually exists in the context bundle assembled in node 1.
An LLM judging another LLM's citations is unfalsifiable and would not survive a
compliance review. Deterministic verification is the entire point of the project.

**Every LLM call is schema-validated with one retry on invalid JSON.** Second failure
fails the node loudly. No silent fallbacks.

### Prompt construction is prefix-stable

Build every prompt as `[system][retrieved guidance][case-specific data]`, in that order,
never interleaved. DeepSeek's cache-hit input rate is ~31x cheaper than cache-miss, and
it only applies to a shared prefix. Log `prompt_cache_hit_tokens` and
`prompt_cache_miss_tokens` from the usage object — cache hit rate is a reported eval
metric, not just a cost lever.

`deepseek-chat` and `deepseek-reasoner` were retired 2026-07-24. Any example using them
is stale.

---

## 6. Eval harness

This is the part almost nobody builds, and the reason this project is worth more than a
working demo. It is also what makes the citation verifier a measured claim rather than an
assertion — a project about verification that doesn't verify itself fails its own thesis.

**8 fixtures.** Alerts with known ground-truth typology. Enough to measure, not enough to
become a labeling project.

**`make eval`. No CI integration.** Actions secrets, billing, and workflow debugging are
the fiddly part and nobody reads the config. Run it manually.

| Metric | Target |
|---|---|
| Typology classification accuracy | report, no fixed target |
| Citation validity rate | **100%** — any unresolvable citation is a hard failure |
| Hallucinated entity rate | **0** — no entity in a narrative that isn't in the context bundle |
| p50 / p95 latency per case | report |
| Cost per case (tokens) | report |
| Prompt cache hit rate | report |

**The results table goes in the README.** That table is the artifact that earns the
signal. A full run costs ~$0.04, so there is no cost argument for skipping it.

---

## 7. UI

Four screens. Nothing else.

1. **Alert queue** — sortable by GNN risk score.
2. **Case view** — context bundle, typology, drafted narrative with each sentence's
   citations visible on hover.
3. **Subgraph** — `react-force-graph`, nodes colored by risk score, for this case only.
   No search, no filters, no layout controls.
4. **Approve / reject** — resumes or kills the LangGraph run.

No auth. Single-tenant demo. Do not build a login system.

---

## 8. Bottlenecks and tradeoffs

| Bottleneck | Reality | Mitigation |
|---|---|---|
| LLM latency | 2 calls × 3–8s dominates everything | Async worker; only 2 LLM nodes by design |
| DeepSeek rate limits / availability | Real concurrency ceiling; provider has had capacity issues | Cap worker concurrency; retry with backoff; surface failure to UI, don't silently hang |
| k-hop subgraph extraction | The actual PyTorch-side cost, not the forward pass | Index the transaction table on both endpoints; cap hops at 2 |
| Human gate | Case pauses hours or days | Postgres checkpointer from day one, never in-memory state |
| Fargate cold start | Large PyTorch image slows task pull | CPU-only torch wheels; the CUDA build is ~2.5GB and useless on Fargate |
| Spot interruption | Worker can be killed mid-run | Safe *because* of the Postgres checkpointer — the human-gate design pays for itself twice |

**Known tradeoff to be able to defend in interview:** a real bank cannot send transaction
data to a third-party foreign LLM API — data residency and regulatory constraints force
self-hosting. This build uses the DeepSeek API for cost; the production path is the same
class of model self-hosted on vLLM inside the VPC. Nothing else in the architecture
changes.

**Second tradeoff:** the IBM dataset is synthetic. Real transaction graphs are messier,
labels are weaker (most laundering is never labeled at all), and class imbalance is worse.
Say this out loud before anyone asks.

---

## 9. Cost posture

**This is not a 24/7 service. `terraform apply` before a demo, `terraform destroy`
after.** Left running, the stack is ~$155/mo — ALB, NAT Gateway, and CloudWatch keep
billing even when every task is stopped. On-demand, it's a rounding error.

| Posture | Cost |
|---|---|
| Full stack, per hour | ~$0.185 |
| Stripped (no NAT, no ALB), per hour | ~$0.12 |
| 20 demo sessions across a job hunt | ~$7–11 |
| Persistent floor when destroyed (ECR, S3) | ~$2–4/mo |
| DeepSeek, entire 8-week build | ~$25 |
| GNN training (local, or spot GPU ~10h) | ~$0–2 |

Rules:

- **Budget alarm at $20 before the first `apply`.** Not after the first surprise.
- **NAT Gateway ~$32/mo is the silent killer.** Public subnet + tight security groups for
  the demo, or VPC endpoints for ECR/CloudWatch/S3. Know this is a demo-only compromise
  and be able to say what production would do instead.
- **CloudWatch log retention: 7 days.** Default never expires.
- **Fargate Spot for the worker** — ~70% off, safe because of checkpointing.
- **Rehearse the spin-up once.** Not for the first time in front of an interviewer.
- **Check for orphaned Elastic IPs** after a destroy; unattached ones bill at $0.005/hr.

### Two Terraform layers

Split the infra into two root modules with separate state. Ephemeral reads persistent via
a remote state data source.

| Layer | Contains | Lifecycle | Cost |
|---|---|---|---|
| **Persistent** | VPC, subnets, security groups, ECR, S3 (model artifact + seed data), IAM roles, budget alarm | Applied once, left alone | ~$2–4/mo |
| **Ephemeral** | ECS services, ALB, NAT Gateway, RDS, ElastiCache | `apply` before a demo, `destroy` after | ~$0.185/hr |

Everything expensive is in the ephemeral layer. Everything slow to rebuild is in the
persistent one. RDS is ephemeral, which is exactly why the reduced seed dataset below is
required rather than optional.

### Seed a reduced demo dataset

~50k transactions covering the 8 eval fixtures and the demo alert queue, loading in under
two minutes. Full dataset stays offline for training only. Without this, every spin-up
re-loads millions of rows and a 10-minute deploy becomes 40. This also makes the repo
clonable by someone else.

### Price volatility

DeepSeek cut prices in May 2026 and raised them in August, and has signaled a further
increase. Peak hours are 01:00–04:00 and 06:00–10:00 UTC on weekdays — 09:00–12:00 and
14:00–18:00 MYT, i.e. the working day. Run eval batches after 18:00 MYT or on weekends for
half price. Do not hardcode cost figures in the README; report measured token usage.

---

## 10. Schedule (6–8 weeks)

| Week | Deliverable | Done when |
|---|---|---|
| ~~1~~ **DONE** | Data + GNN baseline | ✅ GAT 0.4009 test F1 vs XGBoost 0.1817. Gate passed 2026-09-04 |
| ~~2~~ **DONE** | Explainability + batch scoring | ✅ 57,019 alerts in Postgres from model score; attention + SHAP extractable. 2026-09-04 |
| ~~3~~ **DONE** | API + worker + nodes 1–2 | ✅ Cases run end to end via FastAPI → Redis → arq → LangGraph. 2026-09-04 |
| ~~4~~ **DONE** | RAG corpus + nodes 3–4 | ✅ FinCEN ingested to pgvector; narratives draft with sentence-level citations. Corpus incomplete — see §13. 2026-09-04 |
| ~~5~~ **DONE** | Verifier + human gate + evals | ✅ Citation validity 100% (49/49), 0 hallucinated entities; run pauses at the gate and resumes. 2026-09-04 |
| 6 ← **next** | UI | Four screens, subgraph renders |
| 7 | Terraform + deploy | Running on Fargate |
| 8 | Buffer | README, demo script, before/after metrics |

Week 8 is buffer, not scope. Something in weeks 1–7 will overrun.

---

## 11. Repo layout

```
aml-copilot/
  ml/
    dataset.py        # IBM CSV → PyG
    model.py          # GAT
    train.py
    explain.py        # attention + SHAP
    score_batch.py    # → alerts table
  agent/
    graph.py          # LangGraph definition
    nodes/
    schemas.py        # pydantic, one per LLM node
    verify.py         # citation verifier
  api/
    main.py
    worker.py
  rag/
    ingest.py         # fixed corpus → pgvector
  ui/
  eval/
    fixtures/         # 8 labeled alerts
    run_evals.py      # make eval — no CI
  infra/
    persistent/       # VPC, ECR, S3, IAM — applied once
    ephemeral/        # ECS, ALB, NAT, RDS, Redis — apply/destroy per demo
```

---

## 12. Status — Weeks 1–5 complete (2026-09-04)

Built, 384 lines across five files. Nothing downstream scaffolded.

```
ml/dataset.py    IBM CSV -> PyG graph, + neighbour sampler
ml/model.py      2-layer GAT
ml/train.py      training loop, early stopping, -> artifacts/model.pt
ml/baseline.py   XGBoost on 28 flat features
ml/metrics.py    illicit F1 + AUC-PR
```

`make train` / `make baseline` / `make smoke`. Results in §4.

**Graph construction as actually built:** transactions are nodes. Two transactions are
linked if they touch the same account and are adjacent in time on that account, up to 3
positions apart, both directions. Every transaction touches two accounts, so a node gets
temporal neighbours on both its sender and receiver side. Produces pass-through chains
and structuring bursts at O(n) edges instead of blowing up quadratically on hub accounts.
5.08M nodes, 54.0M edges.

**Week 2 added:**

```
ml/explain.py      attention weights (GAT) + SHAP (baseline)
ml/score_batch.py  batch score -> Postgres alerts table
scripts/watch_training.py   live training view, parses the run log
```

`make score` populates `alerts`. `make tensorboard` for run curves. Postgres 16 +
pgvector runs in Docker:

```
docker run -d --name aml-pg -e POSTGRES_PASSWORD=aml -e POSTGRES_USER=aml \
  -e POSTGRES_DB=aml -p 5432:5432 pgvector/pgvector:pg16
```

**Alerts table, 57,019 model-originated alerts** (threshold 0.7682, all 5.08M scored):

| Split | Alerts | Real laundering | Precision |
|---|---|---|---|
| test | 8,897 | 1,145 | 0.129 |
| val | 1,647 | 195 | 0.118 |
| train | 46,475 | 1,319 | 0.028 |

Train precision being *worse* than test is a good sign — the model is not memorising
its training period. The demo queue should filter to `split = 'test'`; train rows are
in-sample and their scores are not honest.

**Week 3 added:**

```
agent/schemas.py                  typology enum, confidence bounded 0-1
agent/state.py                    case state across the graph
agent/llm.py                      OpenAI-compatible client, retry, usage
agent/nodes/gather_context.py     node 1 - evidence bundle, no LLM
agent/nodes/classify_typology.py  node 2 - LLM, schema-validated
agent/graph.py                    LangGraph, fixed edges, Postgres checkpointer
api/main.py                       POST /cases, GET /cases/{id}, GET /alerts
api/worker.py                     arq worker
```

`transactions` table loaded (5,078,345 rows, indexed on both endpoints per §8) so
node 1 pulls history from SQL rather than every worker holding a 5M-row dataframe.
Config lives in `.env`, loaded by the Makefile — see §13.

**Week 4 added:**

```
rag/ingest.py                     corpus -> pgvector, local embeddings
agent/nodes/retrieve_guidance.py  node 3 - pgvector query, no LLM
agent/nodes/draft_narrative.py    node 4 - LLM, sentence-level citations
```

Embeddings are local (`Qwen3-Embedding-0.6B`, 1024-dim, 32k context). Measured on
the dev box: 205ms/encode, 0.7GB RSS, 1.1GB disk — faster and barely heavier than
`bge-base` while carrying 64x the context. Node 3 embeds a query on every case, so
an embedding API would put a rate limit in the path of every investigation.

The graph now short-circuits to END when typology is `none`: no suspicious pattern
means no report, and drafting one anyway manufactures suspicion the classifier did
not find.

**Week 5 added:**

```
agent/verify.py                node 5 - deterministic, no LLM
agent/nodes/human_review.py    node 6 - LangGraph interrupt
eval/fixtures.json             8 alerts, labelled by inspecting the pattern
eval/run_evals.py              make eval
```

`POST /cases/{id}/decision` resumes a run parked at the gate. Verified: a case
stops at `awaiting_review` with 7 checkpoints in Postgres, then resumes to
`approved`.

### Eval results — qwen/qwen3.8-27b on Groq, 8 fixtures

| Metric | Target | Result |
|---|---|---|
| Typology accuracy | report | **2/8 (25%)** |
| Citation validity | **100%** | **100.0% (49/49)** |
| Hallucinated entities | **0** | **0** |
| Escalated after retry | report | 0 |
| p50 / p95 latency | report | 21.6s / 82.0s |
| Tokens per case | report | 4,108 in, 450 out |
| Prompt cache hit rate | report | 0.0% |
| Provider errors | report | 0 |

### Next task — Week 6 only. Do not scaffold anything else.

Four screens: alert queue, case view with citations on hover, subgraph, approve
/ reject. No auth. Read §13 first — the typology classifier is the weak part and
the UI must not present its output as more certain than it is.

---

## 13. Findings

Things learned by building it that were not knowable from the plan.

---

### Week 6 — the evidence horizon

#### The horizon change cost nothing and fixed one thing, for a traceable reason (2026-09-22)

Re-ran the harness after the evidence horizon moved to prior-only. Typology went
3/8 to 4/8, and **the entire gain is one fixture, by a mechanism rather than a
lucky sample.**

| alert | expected | before | after | |
|---|---|---|---|---|
| 5077931 | layering | `RuntimeError` | **layering** | **fixed** |
| 5077723 | layering | rapid_movement | none | reshuffled |
| 4987170 | smurfing | rapid_movement | layering | reshuffled |
| the other five | | | | identical |

5077931 used to fail drafting by writing an eighth sentence past the 300-char
`NarrativeSentence.text` cap, twice. Restricting history to prior transactions
cut its bundle from 42 transactions to 19; the narrative got shorter and stopped
tripping the cap. The sentence cap is not fixed — this case just stopped
reaching it. **Provider errors went 1 to 0: the first run in which all eight
fixtures completed.**

The two reshuffles are the non-determinism, not progress. Their evidence did not
change between runs and their answers did. Anyone reading 3/8 -> 4/8 as a trend
is reading noise plus one real fix.

Citation validity held at 100% across 158 citations in 6 narratives. The two
that drafted nothing both classified `none` and short-circuited, which is the
design working, not a failure.

#### Confidence is not merely uncalibrated, it is inverted

Worth restating with this run's numbers, because it is sharper than "not
calibrated" and it constrains the UI.

| confidence | outcome |
|---|---|
| 0.82, 0.80, 0.70 | all **wrong** |
| 0.65, 0.62, 0.62, 0.60 | all **right** |
| 0.52 | wrong |

**Every answer above 0.65 was wrong.** The four correct ones sit in a tight
0.60-0.65 band. A triage rule of "review the low-confidence ones first" would
beat the obvious one on this sample. That is almost certainly not a stable
signal to exploit, and it is a hard argument against surfacing confidence as
anything an analyst can act on. The UI shows the number and labels it unusable
rather than hiding it, because hiding it invites someone to ask for it later.

#### The cache hit rate moves with evidence size

74.2% on the previous run, 58.0% on this one, with no prompt-construction change
between them. The prefix-stable build in §5 works; the ratio just tracks how much
per-case evidence sits after the stable prefix. Report the mechanism, not the
number.

#### Detection time or investigation time: the bundle now answers this, and the answer was never argued for (2026-09-16)

Node 1 ordered account history by `abs(ts - alert_ts)`, nearest first. That takes
the closest transactions in *either* direction, so the evidence bundle contained
transactions that had not happened when the alert fired. Not a rounding error:
restricting to prior history cut 4987170's bundle from 63 transactions to 27 and
5077931's from 42 to 19. **More than half of what the classifier saw on the
laundering hub account was the future.**

Fixed, with one correction. The first fix used `ts < alert_ts`, which also drops
transactions sharing the alert's timestamp — seven of eight fixtures lose at
least one, and for 5077723 the two dropped rows are 5077724 and 5077725, the
same-minute Euro outbound its `layering` label is *defined* by. Four fixture
rationales name a same-minute pairing explicitly. A transfer in the same minute
is simultaneous, not future. The bound is inclusive and the alerted transaction
is excluded by id, which also stopped it being duplicated as a `txn:` fact
alongside its own `alert:` fact.

**The part worth arguing about.** Removing the lookahead silently answered a
design question the project had not asked: *what information set is an evidence
bundle supposed to represent?*

- **Detection time.** What was knowable when the alert fired. Defensible in a
  compliance review — the narrative cannot cite a transfer that had not yet
  occurred — and consistent with the temporal-split discipline the GNN is held
  to in §4.
- **Investigation time.** What an analyst sees opening the case days later,
  which is the whole account including everything after the alert. This is how
  SAR investigations actually work, and it is how the fixtures were labelled.

The repo now implements detection time. The fixtures assume investigation time.
4987170 is labelled `smurfing` on "twelve small incoming deposits ... before any
outflow", and the alerted transaction is one of the deposits — so the dispersal
that makes the pattern laundering is entirely after it. Under the current rule
the classifier is being asked to name a pattern it structurally cannot see. That
is not a bug in either the code or the label; it is the two horizons disagreeing.

**The bundle is now internally inconsistent about this.** `txn:` facts are
strictly prior; `gnn:` facts are not. The edge rule in §4 links transactions
adjacent in time on an account "up to 3 positions apart **in either
direction**", so attention neighbours can post-date the alert. `ml/dataset.py`
sorts by timestamp and resets the index, so node id order is time order — and
for alert 5077454 the neighbour `5077486` is 32 positions later, carrying the
second-highest attention weight in that case. It is in the bundle as a `gnn:`
fact, available to be cited, while a `txn:` fact from the same moment would have
been filtered out.

So one of these is true and the project should say which:

1. Detection time is right, and the GNN subgraph needs the same filter — which
   means retraining, because the edge rule is baked into the graph.
2. Investigation time is right, and the SQL filter should be relaxed back to
   bounded lookahead, keeping only the ordering and self-exclusion fixes.

Option 1 is the more defensible story and much the more expensive: the graph
cache and the 2h42m training run both assume bidirectional adjacency. Option 2
is a comment and a `WHERE` clause.

**Unmeasured.** The eval was not re-run after this change: the DeepSeek account
is out of balance (402, granted_balance 0.00 — the "free signup grant" reported
by pricing aggregators does not exist on it). The last recorded run predates the
horizon change and is no longer a fair baseline for 4987170 or 5077931, whose
bundles more than halved. Structural verification did pass without a provider:
zero post-alert `txn:` facts across all eight fixtures, no alert present in its
own history, 5077723's pair restored.

### Week 5 — verifier, human gate, evals

#### The thesis holds, but on a smaller sample than the number suggests

**Citation validity 100% (49/49), hallucinated entities 0.** Deterministic
verification works: every evidence_id in every drafted sentence resolved to a fact
node 1 assembled, and no narrative named an account outside the bundle.

The caveat that must travel with that number: **only 3 of 8 fixtures produced a
narrative at all.** The other 5 were classified `none` and short-circuited before
drafting. 49 citations across 3 narratives is a real result but a thin one. Report it
as "100% across 3 narratives", never as "100% across 8 cases".

#### Typology classification is the weak part: 25%, and confidently wrong

The model answered `none` for 5 of 8 fixtures at 0.90-0.95 confidence, when exactly
one fixture is `none`.

| alert | expected | predicted | confidence |
|---|---|---|---|
| 5077604 | rapid_movement | none | 0.95 |
| 5077725 | rapid_movement | rapid_movement | 0.95 |
| 5077723 | layering | rapid_movement | 0.95 |
| 4987170 | smurfing | none | 0.90 |
| 5077931 | layering | rapid_movement | 0.85 |
| 5077454 | layering | none | 0.95 |
| 5077772 | layering | none | 0.95 |
| 4385373 | none | none | 0.95 |

Two things follow, and neither is cosmetic:

**Confidence is not calibrated.** 0.95 on wrong answers is as common as on right
ones. It cannot be used for triage or for auto-approving anything, and the UI must
not display it as if it means something.

**Three hypotheses for the `none` bias, untested, in order of suspicion.** (1) The
system prompt says "say none if the pattern is absent" and "same-account transfers
are routine bookkeeping" — both written to fix earlier problems, both pushing toward
`none`. (2) `HISTORY_LIMIT = 25` nearest-in-time transactions is too few: account
15-803DE4A90 has 41 transactions and its pattern is accumulate-then-disperse across
four days, which cannot be seen through a 25-transaction window. (3) The evidence
bundle is a flat list of transactions; the model must infer the pattern with no
aggregate view. Do not tune the prompt without re-running the harness — that is what
it is for.

#### Hypothesis (2) tested and it is the wrong lead (2026-09-11)

Raised `HISTORY_LIMIT` 25 → 100 and re-ran the harness. **Typology accuracy did not
move: 2/8 before, 2/8 after, every one of the eight classifications identical.** That
result means much less than it appears to, and the reason is the finding.

The window was only ever truncating one fixture. Transactions per fixture account:

| alert | account | txns | truncated at 25 |
|---|---|---|---|
| 5077931 | 15-803DE4A90 | **41** | **yes — lost 16** |
| 5077454 | 11128-8045F4500 | 25 | no — exactly at the limit |
| 4987170 | 128590-80ADBB8A0 | 23 | no |
| 5077723 | 14766-805C42580 | 5 | no |
| 5077725 | 14766-805C42580 | 5 | no |
| 5077604 | 2439-80604C300 | 4 | no |
| 5077772 | 24-803D94320 | 4 | no |
| 4385373 | 319127-806F2A7D0 | 2 | no |

Seven of eight accounts hold fewer than 25 transactions, so node 1 was already
returning their complete history. Confirmed rather than assumed: prompt tokens came
back **byte-identical** across the two runs for all seven — 1,410 → 1,410, 4,447 →
4,447, 5,081 → 5,081. A wider window handed them nothing because there was nothing
left to hand them.

The one account the limit did bind on is 15-803DE4A90 — the same account that
motivated the hypothesis. At limit 100 its request returned **HTTP 413, request too
large**, on the Groq free tier. At 25 it was already 10,810 prompt tokens; all 41
transactions is roughly 17-18k, and a limit of 50 would fail the same way.

**So hypothesis (2) is untested, not disproven.** The distinction matters and the
phrasing in a writeup must hold it: the limit affected one case, and that case did not
run. Testing it needs a provider without the free-tier request cap — about $0.003 for
the single case on DeepSeek.

Two consequences for where to look next:

**Hypotheses (1) and (3) are now the live ones.** Five fixtures were classified `none`
while the model could already see every transaction on the account. Whatever is going
wrong there is not truncation. (1) is far cheaper to test.

**A fourth hypothesis, not in the original three.** Node 1 gathers history for the
*source* account only — the query passes `src` twice and never touches `dst`. The
destination account's onward movement is absent from the bundle. Four fixtures expect
`layering` and all four are wrong; two of those accounts (5077723, 5077772) hold 5 and
4 transactions. For those, whatever makes the case layering is not in the evidence at
all and no window size can put it there. Layering is a chain across accounts; the
bundle covers one account.

**Fixture design lesson.** The eight fixtures were chosen for their laundering pattern,
with no attention to how much account history each one carries. That makes the set
structurally unable to test any context-window hypothesis — seven of eight cannot
distinguish a limit of 25 from a limit of 1000. A fixture set meant to test evidence
sufficiency has to be selected on history depth as well as on typology.

Reverted to `HISTORY_LIMIT = 25`; the run containing the 413 was not kept as
`eval_results.json`, since a provider error in the file corrupts the citation
denominator that the README quotes.

#### Hypothesis (4) confirmed: node 1 gathered the wrong account (2026-09-13)

Node 1 built its history from `src` twice — `WHERE src_account = %s OR dst_account
= %s` with `(src, src, ...)`. The alerted transaction's counterparty was never
gathered. Fixture 4987170 is the case that proves it matters: the alert is
`128590-80ADBB8A0 -> 15-803DE4A90`, and the smurfing pattern the label refers to
sits on the **destination**. The bundle held the sender's 23 transactions and none
of the 40 on the receiving account. The model answered `none`, which given that
evidence is arguably right.

Changed the query to span both endpoints. Bundle for 4987170 went 23 -> 63, and the
classifier's own reasoning now reads "Account 15-803DE4A90 acts as a hub: it
receives large Yen inflows from many distinct accounts ... and then sends funds to
numerous external accounts" — the accumulate-then-disperse pattern, described from
transactions that were physically absent before. It still mislabels it (`smurfing`
expected), but it stopped refusing to classify.

Direction had to change with it. `"outgoing" if h[2] == src` was correct only while
history covered one account; with both endpoints it labels every outbound transfer
from the destination as "incoming". Direction is now relative to whichever endpoint
the transaction touches, and each entry carries an `account` field naming it.

Run against `deepseek-flash`, both endpoints, `HISTORY_LIMIT = 100`:

| Metric | Groq, src-only | DeepSeek, both ends |
|---|---|---|
| Typology accuracy | 2/8 (25%) | 3/8 (38%) |
| Narratives drafted | 3/8 | **6/8** |
| Citation validity | 100.0% (49/49) | **100.0% (165/165)** |
| Hallucinated entities | 0 | 0 |
| Classified `none` | **5 of 8** | **1 of 8** |
| p50 / p95 latency | 21.6s / 82.0s | 44.7s / 60.6s |
| Tokens per case | 4,108 in, 450 out | 7,374 in, 3,249 out |
| Provider errors | 0 | 1 |

**Do not quote the accuracy number.** One fixture of movement on a provider that is
not deterministic (below), from a single run of eight. The two results large enough
to survive that: the `none` collapse cleared 5 -> 1, and citation validity now rests
on 165 citations across 6 narratives instead of 49 across 3. §13's standing caveat
that the thesis is measured on 3 narratives is much weaker than it was.

Three variables moved at once — evidence, provider, token budget — but the
per-fixture detail separates them:

| alert | bundle | outcome | attributable to |
|---|---|---|---|
| 4987170 | 23 -> 63 | `none` -> `rapid_movement`, still wrong | evidence |
| 5077454 | 25 -> 26 | `none` -> `layering`, **fixed** | provider/budget, not evidence |
| 5077604 | 4 -> 8 | `none` -> `rapid_movement`, **fixed** | confounded |
| 5077723 | 5 -> 5 | unchanged, wrong | prompt — bundle identical |
| 5077772 | 4 -> 4 | unchanged, wrong | prompt — bundle identical |
| 4385373 | 2 -> 8 | `none` -> `layering`, **broke** | evidence |

**The cost of the fix is a false positive, and it lands on the only true negative in
the set.** 4385373 is two recurring ~100 USD payments a week apart. Pulling in the
counterparty's six transactions gave the model enough benign activity to read as
layering. In AML the false-positive direction is the expensive one — it is what
drowns a compliance team — and the fixture set has exactly one case that tests it.
Before judging this change, the set needs more true negatives.

The two fixtures whose bundles could not change are the two where `src == dst`, and
both are still wrong. Evidence held constant, provider and budget varied, answer the
same. That isolates them to hypothesis (1) — the system prompt's "same-account
transfers are routine bookkeeping" clause, which 5077723 was deliberately built to
probe.

5077931 classified but failed drafting: the model wrote an 8th sentence past the
300-char `NarrativeSentence.text` cap, twice. The cap was left alone — relaxing it
changes what the verifier operates on.

Measured cost: ~59k in / ~26k out per run, about $0.025 off-peak on `deepseek-flash`.

#### Reasoning models break "the provider is a config change"

`.env.example` claims Groq / Gemini / DeepSeek / local vLLM are a base_url and a
model name with no code change. That is false for a reasoning model.

`deepseek-flash` emits `reasoning_content` and **bills it against `max_tokens`**.
The 400/900 budgets set for Groq's 1000 tok/min free-tier cap left nothing for the
answer: the response came back with `finish_reason: length`, ~3,380 tokens of
chain-of-thought, and `content` as an **empty string**. The schema layer then
reported `Invalid JSON: EOF while parsing` — a parse error three layers from the
cause, and the retry hit the identical wall. Two full runs were aborted before the
cause was visible.

Raised to 8000 for both nodes. `max_tokens` is a cap, not a reservation — unused
headroom is not billed — so there was never a reason to raise it incrementally.

Two consequences worth stating:

**A cramped budget degrades the answer, not just the parse.** At 2000, fixture
5077604 classified `layering`; at 8000, same evidence and same `temperature=0`, it
classified `rapid_movement` — correctly. A truncated chain of thought reaches a
worse conclusion before it reaches a broken one.

**The config is now DeepSeek-only.** 8000 exceeds Groq's free-tier output cap, so
switching back re-breaks what the 400/900 values existed to avoid. Provider
portability costs a per-provider token budget; it is not free.

Worth adding to `complete_json`: check `finish_reason == "length"` and raise
something that names truncation. The current failure mode is expensive to diagnose.

#### The LLM path is not deterministic at temperature=0

Fixture 4987170, identical evidence bundle, `temperature=0`, `max_tokens=8000`,
same model: `layering` in an isolated call, `rapid_movement` inside the harness run
minutes later.

The README says runs are deterministic. That is true of the GNN — fixed seeds in
torch and numpy, two runs reproduce bit-identically — and **not** true of anything
downstream of an LLM call. An 8-fixture eval read as a single point estimate wobbles
by at least one fixture on resampling alone.

Consequence for the harness: a ±1 accuracy difference is noise. Any typology number
worth quoting needs 3-5 runs and a range, not one run and a percentage. Citation
validity is more robust — it is a property of every sentence drafted, so it
aggregates over 165 observations rather than 8.

#### Hypothesis (1), partially tested: no support, and not a refutation either

Softened one of the two clauses — "Same-account transfers are routine bookkeeping and
are rarely laundering" became "often routine bookkeeping, but not always; judge it by
the transactions around it in time and value, not by the fact that the sender and
receiver match."

Two deliberate choices in that wording. The other clause ("say none if the pattern is
absent") was left alone: the `none` collapse had already cleared 5/8 -> 1/8 by then, so
it was no longer the active problem, and it is the only thing in the prompt guarding
against false positives — which 4385373 had just demonstrated the system produces. And
the replacement does not name the currency-conversion pattern the same-account fixtures
actually contain, because writing the answer into the prompt would make the eval
measure nothing.

Stopped after 4 of 8:

| alert | target | before | after | |
|---|---|---|---|---|
| 5077604 | no | rapid_movement ✓ | rapid_movement ✓ | same |
| 5077725 | no | rapid_movement ✓ | layering | **broke** |
| 5077723 | **yes** | rapid_movement | rapid_movement | unchanged |
| 4987170 | no | rapid_movement | layering | still wrong |

**The change moved the fixtures it was not aimed at and left the one it was aimed at
alone.** 5077723 is purpose-built to probe that clause and did not budge. 5077725,
whose alerted transaction is cross-account, flipped from correct to `layering` —
plausibly because its *history* contains 5077723's same-account transfer, so removing
the dismissal let the model read the neighbouring transfer as a layering signal and
relabel the case. That is a mechanism, not just noise, and it is the wrong direction.

**What this does not establish.** 5077772 never ran, and it is the strongest available
test of the clause: a same-account transfer that the model was answering `none` on,
which is exactly the behaviour the clause predicts. Four fixtures, one sample each,
against a model that gave three different answers across three samples of 4987170.

Read this as "the obvious edit did not help and cost an answer," not as "hypothesis (1)
is dead." Anyone retrying it should run all eight and sample each 3-5 times, or the
result will not mean more than this one does.

Reverted.

#### Citation validity and usefulness remain independent

Worth restating with numbers now: the run scoring 100% citation validity also scored
25% on typology. A perfectly cited narrative about the wrong typology is a perfectly
cited wrong answer. **The verifier proves the narrative rests on real evidence; it
proves nothing about whether the conclusion is right.** That is the honest framing of
the centrepiece.

#### Free-tier limits shape the measurement

Groq's free tier caps output at 1000 tokens/minute, below the SDK's 2048 default, so
requests failed before running until `max_tokens` was set per node (400 classify, 900
narrative). p95 latency of 82s (max 117s) is throttling, not model speed — the §8
design target of 10-20s is not disproven, just not measurable on a free tier.

Prompt cache hit rate came back 0.0%. Either the provider does not report
`cached_tokens` through the OpenAI shim or it does not cache at all. §5 treats cache
hit rate as a reported metric; on this provider it is unmeasurable, and that should be
stated rather than reported as a genuine zero.

### Week 4 — RAG, narrative drafting

#### The corpus is incomplete and this is the main gap

Only FinCEN's SAR narrative guidance is ingested (33 pages, 48 chunks). **FATF and
FFIEC both sit behind Cloudflare bot protection** and return 403 to any automated
fetch. Working around bot protection was not attempted.

Consequence, measured: a query for "rapid movement of funds" retrieves at 0.49
similarity, against 0.81 for a SAR-narrative query. The corpus answers *how to write
a report* but says nothing about *what each typology looks like*. Retrieved guidance
scored 0.54-0.57 on a live case and **was cited zero times** in the narrative.

**RAG is wired and working but not yet earning its place.** To fix, download by hand
and drop into `data/corpus/` (ingest picks up any PDF/TXT/MD):

- FATF Trade-Based Money Laundering Risk Indicators (2021)
- FATF Virtual Assets Red Flag Indicators
- FFIEC BSA/AML Appendix F — Money Laundering and Terrorist Financing Red Flags

Until then, do not claim the typology classifier is grounded in regulatory guidance.
It is grounded in the system prompt.

#### The narrative first came out as a data dump

Node 4's first output spent 5 of 11 sentences reciting GNN attention weights,
neighbourhood sizes and risk scores. Every sentence was correctly cited and every id
resolved — and it was useless to an analyst, who needs the transaction pattern, not
the model's arithmetic.

Fixed in the prompt: use attention to decide *which* transactions matter, then write
about those transactions. After the fix the same class of case produced five
sentences about money and accounts, correctly flagging a same-account transfer as
such. **Citation validity and narrative usefulness are independent properties** — the
week 5 verifier measures the first and says nothing about the second.

#### Providers disagree on the same case

Alert 5077604 was classified `rapid_movement` by Gemini and `none` by Qwen3-32B on
Groq, from an identical evidence bundle. Not a bug — a real measurement of provider
variance, and an argument for the week 5 eval harness running the same fixtures
across providers rather than picking one on vibes.

### Week 3 — API, worker, nodes 1–2

#### `Is Laundering` labels a transaction; a typology describes a pattern

The most consequential finding of week 3, and it changes week 5.

Case 5077724 was classified `rapid_movement` and scored as a false positive against
the CSV label. Inspecting the account says otherwise:

| txn | time | amount | label |
|---|---|---|---|
| 5077604 | 09-11 19:36 | 876,934,779 Rupee in | **LAUNDER** |
| 5077723 | 09-12 10:14 | 886,180,041 Rupee (self) | — |
| 5077724 | 09-12 10:14 | 107,427 EUR out | — |
| 5077725 | 09-12 10:14 | 10,189,751 EUR out | **LAUNDER** |

About $10.5M arrives, rests 15 hours, and leaves within one minute across three
transactions — two labelled, one not. The model's answer is defensible; the label is
incomplete.

**Consequence:** scoring typology classification against `Is Laundering` systematically
punishes correct answers, because the column labels transactions while a typology
describes a flow. The 8 eval fixtures must carry typology labels assigned by inspecting
the pattern, not inherited from the CSV. Budget time for that in week 5 — it is a
labelling task, not a coding one.

#### The citation verifier cannot catch faulty reasoning

Case 4385373 was classified `structuring` on this reasoning: two identical payments of
9,567.48 **Ruble** are "just below the typical 10,000 reporting threshold". The $10,000
CTR threshold is US dollars; 9,567 Ruble is roughly $100. The model applied a US rule to
a foreign-currency amount without converting, and called two identical weekly payments
"structuring" when that pattern is a recurring payment.

Every evidence id in that reasoning resolves. The evidence is real; the inference is
wrong. **`verify_citations` catches fabricated evidence, not faulty inference** — worth
saying out loud before an interviewer finds it, because §5 calls the verifier the
centerpiece. Week 4's retrieved guidance should reduce this class of error by supplying
real thresholds; the human gate is what actually catches it.

#### Free-tier rate limits are the real concurrency ceiling

§8 predicted this and it still cost a cycle. `max_jobs=4` against Gemini's free tier
produced 429s on every case; sequential runs with backoff still exhausted the daily
quota. Now `max_jobs=1` (env-overridable) and the SDK retries 429/5xx with exponential
backoff, kept separate from the schema retry — a rate limit is not malformed JSON and
must not consume the one retry §5 allows.

Provider is `AML_LLM_BASE_URL` + `AML_LLM_MODEL`, so the Gemini → Groq switch was config
only, no code change. That design held. What failed was operational: environment
variables do not cross terminal tabs, and three cycles were lost to a worker running
without the config it was supposed to have. Config now lives in `.env`, loaded by the
Makefile, gitignored. **Verify config in-process (`ps eww <pid>`) before trusting a run.**

#### Week 2 — explainability, batch scoring

#### F1 is the wrong headline metric here — lead with AUC-PR

The single most important thing learned so far. Two models that are statistically
indistinguishable produced wildly different F1:

| | Before edge fix | After edge fix |
|---|---|---|
| Val F1 | 0.2237 | 0.2269 |
| **Test AUC-PR** | **0.3183** | **0.3127** |
| Test F1 | 0.4009 | 0.2141 |
| Test precision | 0.3039 | 0.1287 |
| Test recall | 0.5888 | 0.6372 |

Test F1 halved while AUC-PR — which is threshold-independent — barely moved. The cause
is threshold transfer: the cutoff is picked on 524 validation positives and does not
survive the shift to a test period with a different illicit rate (0.103% vs 0.177%).
A 0.09 change in threshold moved precision from 0.30 to 0.13.

**Consequence for Week 5:** the eval harness must lead with AUC-PR and report F1 only
alongside the threshold that produced it. An eval on 8 fixtures has far less signal than
524 positives, so F1-only comparisons there will manufacture wins and losses that are not
real. Report the threshold as part of every result.

#### The self-transfer bias was an edge-construction bug, and fixing it was not enough

Root cause found: a same-account transfer entered its account's timeline **twice under
the same key**, paired with itself, and those pairs were dropped as self-loops. It burned
its adjacency slots on nothing. Fixed — it now enters once.

Effect on the alert queue (test period, ranked by risk score):

| Slice | Self-transfers before | After | Hit rate before | After |
|---|---|---|---|---|
| Top 100 | 56 | 41 | 37% | 47% |
| Top 500 | 184 | 163 | 56% | 57% |
| Top 2000 | 588 | 457 | 44% | 42% |

Real but partial. Self-transfers in the top 2,000 fell 22% and the top-100 hit rate rose
10 points, but **the inverted ranking survives** — top-500 still beats top-100, which
should never happen. And AUC-PR did not move, so overall ranking quality is unchanged;
the fix redistributed the head of the queue without making the model better.

The residue is not fixable by better code: 12.4% of self-transfers are the only
transaction on their account, so they have no neighbours and the GAT has no context to
use. They fall back on raw features, where the huge amounts dominate. If this needs to go
further, it is a feature-engineering problem (give the model its own degree, so it can
learn to distrust isolated nodes), not a graph-construction one.

#### The baseline wins on payment format, not on the account aggregates

SHAP over 300 real laundering transactions puts `format` at +1.96, roughly 4x the next
driver. `passthrough`, `velocity` and `fan_ratio` — the engineered features the baseline
was built around in §4 — contribute weakly and negatively. It learned "ACH is
suspicious" (1,653 of 1,797 test-period laundering transactions are ACH).

Worth saying out loud when presenting the GAT-vs-baseline comparison: the baseline is not
losing because its hand-engineered features are weak, it is losing while barely using
them.

#### Operational: never let a smoke run share an artifact path with a real one

A 3-epoch smoke test overwrote a 2.8-hour trained model, because `train.py` saved to a
hardcoded `artifacts/model.pt` regardless of dataset. Cost: a full retrain. The output
path is now keyed on the dataset name. Fixed seeds meant the retrain reproduced the
destroyed run bit-identically, which is the only reason the loss was just time.

Applies forward: every artifact path in weeks 3–7 should carry the identity of what
produced it.

---

### Week 1 — data, GAT, baseline

#### The model has a self-transfer bias — *resolved in Week 2, see above*

In the test period there are 21,441 transactions where an account sends to itself.
**Exactly 5 are laundering.** The model put 588 of them in its top 2,000 by risk score,
and 56 in its top 100. Over a quarter of its most confident alerts are a category that is
almost never laundering.

It shows up as an inverted ranking — the hit rate *improves* as you go down the queue,
which should never happen:

| Slice of alert queue | Real laundering |
|---|---|
| Top 100 | 37% |
| Top 500 | **56%** |
| Top 2000 | 44% |

The model is given a `self_txn` feature and weighted it wrong, most likely because these
are large-amount ACH entries and amount is a strong signal. This is a concrete, fixable
defect, and it is the first thing Week 2's attention weights should be pointed at — if
explainability cannot surface a known bias, it will not surface an unknown one.

Baseline for comparison: random selection hits laundering 0.18% of the time. The model's
top 500 hits 56%, roughly 300x better than chance. The ranking is good; the very top of
it is polluted.

#### Class weighting: raw ratio beats damping

`pos_weight` ablation, selected on validation, never on test:

| pos_weight | Val F1 | Test F1 |
|---|---|---|
| 1244 (raw neg/pos) | **0.2237** | **0.4009** |
| 35 (sqrt damping) | 0.1361 | 0.2489 |

Damping lost decisively. At 1 positive per 980 the strong weight earns its keep. It also
did **not** cause the jagged validation curve it was suspected of — the curve stays jagged
under both settings.

#### The noise floor limits how finely anything can be tuned

The validation split holds only 524 illicit transactions. Epoch-to-epoch F1 differences
below roughly **0.03 are not meaningful**. Carry this into Week 5: an eval harness on 8
fixtures has the same problem far worse, so treat per-fixture typology accuracy as
directional, not as a number to optimise against.

#### Environment constraints (local dev is a 16GB M4 Mac, not Linux+GPU)

- **PyG's `NeighborLoader` is unusable here.** It requires `pyg-lib` or `torch-sparse`,
  neither of which ships a macOS-arm64 wheel for current torch. `ml/dataset.Sampler` is a
  ~30-line replacement. Full-batch GAT does not fit past ~50k transactions on 16GB, so
  mini-batching is not optional.
- **`brew install libomp`** is required or xgboost will not import on macOS.
- Graph construction over 5M rows takes minutes, so `load()` caches to a `.graph.pt`
  (1.7GB, 14s → 0.6s). Do not commit it.
- Training run: ~5 min/epoch, ~97 min to early stopping. Budget accordingly; this is the
  slow loop in the whole project.
- The IBM CSV is **not** sorted by time. Taking the first N rows gives a degenerate
  temporal split (train covering 11 hours, test covering 9 days). Always sort first.

---

### Scope note

Weeks 2–8 are unchanged and unvalidated. Everything above §13 that has not been built
yet remains a plan, not a finding.
