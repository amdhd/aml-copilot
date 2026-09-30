# Demo script — 8 minutes

For an interview: a screen share of the live system, then questions. The spoken lines
are a guide, not a script to recite. Every number here is in the README; if the README
changes, this changes with it.

---

## Before the call (T–20 min)

1. **Run it from the machine you will present on.** The ALB admits only the IP `demo-up`
   runs from. If the interviewer should reach it too, add theirs:
   `EXTRA_CIDRS=<their-ip>/32 make demo-up`.
2. **`make demo-up`** (~6–8 min). Wait for `worker ready (0 restarts)` and the URL. The
   browser asks for your reviewer login; the case view will say who approved.
3. **Pre-run three cases** from the queue, so nobody watches a 30–60s wait:
   - **5077931** (queue #8) — the clean example. Usually `layering`, cites red flags.
   - **5077724** (queue #2) — `rapid_movement`, the pattern is easy to see.
   - **4385373** (queue #17) — labelled `none` in the eval (a recurring ~100 USD payment).
   The classifier is not deterministic: note what each one actually said, and use the
   *Cases* tab to reopen them. If 4385373 comes back `none`, it got it right — see
   step 5 for what to say either way.
4. Leave one alert un-run (**5077604**, queue #3) to open live.
5. Open the README on GitHub in a second tab, scrolled to the architecture diagram.
6. Outside 09:00–12:00 and 14:00–18:00 MYT if you can: DeepSeek is slower at peak.

---

## 1. The problem — 45s

*Screen: README, top.*

> Compliance analysts at a bank work a queue of alerts. Each one means pulling the
> account's transaction history, working out which laundering pattern it looks like,
> checking that against regulatory guidance, and writing a Suspicious Activity Report.
> This system does the gathering and drafting. The analyst decides — nothing is filed
> automatically.

Say early, before anyone asks: **the data is IBM's synthetic AML dataset**, 5 million
transactions. Real data is messier and most real laundering is never labelled.

## 2. Detection — 1 min

*Screen: the alert queue.*

> A graph attention network scores every transaction. Transactions are the nodes,
> linked when they touch the same account close together in time — so the model sees
> a lump arriving and being split up hours later, not one odd payment.

- Headline metric is **AUC-PR, 0.31 vs 0.12** for an XGBoost baseline that gets 18
  hand-built account features — 2.7x. Not F1: F1 moves with the threshold, and on
  this data the threshold doesn't transfer from validation to test.
- In plain terms: **1 in 565** test transactions is laundering. In the top 500 of this
  queue it's **57%** — roughly 300x better than picking at random.
- Point at row #1: **it's a same-account transfer.** Self-transfers pollute the top of
  the queue; it's a known issue in the README, partly fixed, and the rest is feature
  engineering, not a bug.

## 3. One investigation — 2 min

*Screen: open 5077604 live — click Investigate. Talk while it runs (~30–60s).*

> Six steps, fixed order, two of them call an LLM. Gather evidence from Postgres and
> the GNN's attention. Classify the typology. Retrieve red flags from FATF, FFIEC and
> FinCEN in pgvector. Draft the narrative. Verify it. Stop and wait for a human.

Why fixed rather than a free-roaming agent:

> A regulator has to be able to replay an investigation. An agent that picks a
> different path each run isn't auditable.

*When it lands — or switch to pre-run 5077931:*

- **Typology panel:** read the warning aloud. *"It's a suggestion. The classifier is
  right about half the time on my labelled cases, and its confidence isn't calibrated —
  so the UI says not to triage on it."*
- **Narrative:** hover a `txn:` citation — it resolves to the actual transaction. Hover
  a `guidance:` one — it's a red flag, and it always sits beside transaction ids.
- **Subgraph:** the neighbours the GNN attended to. Grey ones scored below the alert
  threshold.

## 4. The verifier — 1 min

*Screen: the green line above the narrative.*

> This is the part I'd defend hardest. Every citation is checked in plain Python, not
> by another LLM: every evidence id has to exist in what the pipeline gathered, every
> account in the prose has to be in the evidence, and no sentence can rest on a red
> flag alone. An LLM grading an LLM isn't falsifiable; this is.

Numbers: **186 of 186 citations resolved, zero invented accounts** across the eval.

## 5. What the verifier can't do — 1 min

*Screen: 4385373 from the Cases tab.*

If it said `layering`:

> This one's labelled *not* laundering — two identical 100-dollar payments a week apart.
> The model said layering, and three sentences of the narrative cite a real FinCEN
> layering red flag beside real transactions — every citation checks out. A perfectly
> cited narrative about the wrong conclusion is a perfectly cited wrong answer.
> Citations make it *look* better grounded; they don't make it right. That's why a
> human approves.

If it said `none`:

> This one's a recurring 100-dollar payment, labelled not laundering, and it got it
> right — so there's no narrative. On other runs it's said layering and cited a real
> layering red flag for it, three times. That's the failure I worry about: a wrong
> answer that verifies.

## 6. The human gate — 45s

*Screen: 5077724 or 5077931, click Approve.*

> The run has been paused in Postgres since it drafted. It could wait days — that's
> normal for a case. Approve resumes it from the checkpoint.

Status flips to `approved` within a few seconds.

## 7. Infrastructure — 1 min

*Screen: README architecture diagram.*

- ECS Fargate, RDS Postgres with pgvector, ALB. Two Terraform layers: the persistent
  one (network, registry, S3, budget alarm) costs about **$0.20 a month**; everything
  that bills by the hour is **~$0.11/hr** and exists only during a demo — `make
  demo-up`, `make demo-down`.
- **No NAT Gateway** — $32/month idle. The embedding model is baked into the image, so
  tasks only talk to ECR, S3 and the LLM. Production would use private subnets and VPC
  endpoints.
- Worker on **Spot**: safe because run state lives in the Postgres checkpointer, not
  the task.

## 8. Close — 15s

> What's weakest is the typology classifier — about half right, and its confidence
> means nothing. What's strongest is that it can't put a sentence in front of an
> analyst that isn't traceable to real evidence.

---

## Questions to expect

**"Could a bank use this?"** Not as is. A bank can't send transaction data to a
third-party LLM API — data residency. The production path is the same class of model
self-hosted inside the VPC; nothing else in the architecture changes.

**"Why is typology accuracy only 50%?"** Eight hand-labelled cases, one draw — it moves
±1 between identical runs because the provider isn't deterministic at temperature 0.
The honest summary is "about half, unstable." The classifier is grounded in its prompt,
not in retrieved guidance: retrieval happens after classification.

**"Why a GNN and not XGBoost?"** AUC-PR 0.31 vs 0.12, and XGBoost got hand-built account
aggregates the GNN never sees.

**"How do you know the narrative isn't made up?"** The verifier — deterministic, with
tests. It proves sentences rest on real evidence. It does not prove the conclusion.

**"What would you do next?"** Calibrate or drop the confidence score; a larger labelled
eval set, since eight cases can't separate real improvements from noise; feed red flags
to the classifier, not just the drafter; give the GNN node degree so it stops trusting
isolated self-transfers.

**"What did it cost?"** About $0.20/month idle; about 11 cents per demo hour; the LLM is
fractions of a cent per case (~7.5k tokens in, ~3.5k out).

---

## If something breaks

| Symptom | Likely cause | Do |
|---|---|---|
| URL doesn't load at all | Your IP changed since `demo-up` | `make demo-up` again: it re-applies for the new IP and reseeds, and keeps cases |
| Login prompt keeps coming back | Wrong password, or reviewers not set | Re-set `/aml-copilot/reviewers` (README, *Deploy*), then `aws ecs update-service --cluster aml-copilot --service api --force-new-deployment` |
| Case sits at `queued` | Worker not up yet | Wait for `worker ready`; `aws logs tail /ecs/aml-copilot --follow` |
| Case `failed` | Often a 402: DeepSeek balance ran out | `aws logs tail /ecs/aml-copilot --since 10m` for the error; top up; switch to the pre-run cases meanwhile |
| Case takes >90s | DeepSeek peak hours | Talk through it, or switch to a pre-run case |

**Afterwards: `make demo-down`.** Left up, it's about $2.60 a day.
