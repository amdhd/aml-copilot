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
| 5 ← **next** | Verifier + human gate + evals | Citation validity measured; run pauses and resumes correctly |
| 6 | UI | Four screens, subgraph renders |
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

## 12. Status — Weeks 1–4 complete (2026-09-04)

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

### Next task — Week 5 only. Do not scaffold anything else.

Node 5 `verify_citations` (deterministic Python), node 6 `human_review`
(LangGraph interrupt), and the eval harness. See the fixture-labelling warning in
§13 before building the fixtures.

---

## 13. Findings

Things learned by building it that were not knowable from the plan.

---

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
