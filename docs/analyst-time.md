# Analyst time: by hand vs with the copilot

The before/after measure for week 8: how long one investigation takes an analyst
working from raw data, against reviewing what the copilot drafted.

**What this can and cannot show.** One analyst, four cases, and the analyst built the
system and is not an AML professional. It is an illustration of where the time goes, not
a measurement of productivity — say so whenever the number is quoted. A real study needs
several analysts and cases they have not seen.

---

## Design

Doing the same alert twice would time the second attempt against an analyst who already
knows the answer. So each method gets a *different* alert of the *same* typology:

| Pair | By hand | With copilot | Order |
|---|---|---|---|
| A — layering | 5077454 | 5077931 | by hand, then copilot |
| B — rapid movement | 5077725 | 5077604 | **copilot first**, then by hand |

Pair A's alerts share no accounts. Pair B's do: 5077604 moves money *into*
14766-805C42580 and 5077725 moves it *out*. Running the copilot first means anything
learned leaks toward the by-hand attempt, which makes the copilot look *less* useful —
the direction to err in.

Do all four in one sitting, in this order: A by hand, A copilot, B copilot, B by hand.
Don't read `eval/fixtures.json` first; it holds the answers.

---

## Setup (local, free)

```bash
docker start aml-pg aml-redis
make api          # terminal 1
make worker       # terminal 2 — wait for "worker ready: GNN loaded"
npm --prefix ui run dev    # terminal 3 -> http://localhost:5173
```

A stopwatch, and this file open to fill in.

---

## By hand — what "done" means

Start the clock when you first look at the alert. Stop it when you have:

1. **A typology call** — structuring, layering, smurfing, trade_based, rapid_movement,
   or none — and one line saying why.
2. **A draft narrative** of at least five sentences, each naming the transaction ids it
   rests on, covering who, what, when and why it is suspicious.

Use only SQL. Look only at transactions **at or before** the alert — the copilot sees
nothing later, and the comparison has to be fair.

```bash
docker exec -it aml-pg psql -U aml -d aml
```

```sql
-- the alert (never SELECT *: is_laundering is the answer)
SELECT txn_id, ts, src_account, dst_account, amount, currency, payment_format
FROM transactions WHERE txn_id = 5077454;

-- both accounts' history up to the alert (swap in the alert's accounts and time)
SELECT txn_id, ts, src_account, dst_account, amount, currency, payment_format
FROM transactions
WHERE (src_account IN ('11128-8045F4500', '48211-811F1C280')
    OR dst_account IN ('11128-8045F4500', '48211-811F1C280'))
  AND ts <= '2022-09-11 13:50:00'
ORDER BY ts;
```

Alert details for the two by-hand cases:

| Alert | Accounts | Time |
|---|---|---|
| 5077454 | 11128-8045F4500 → 48211-811F1C280 | 2022-09-11 13:50 |
| 5077725 | 14766-805C42580 → 1299-8010F8CE0 | 2022-09-12 10:14 |

## With copilot — what "done" means

Start the clock when you click **Investigate**. Stop it when you click **Approve** or
**Reject**, having:

1. Read the suggested typology and decided whether you agree.
2. Read every narrative sentence and hovered its citations, enough that you would sign
   it.
3. Noted any sentence you would change or delete.

Record the wait separately: the time from clicking Investigate to the case appearing.
That is machine time, not analyst time, but it is part of what the analyst sits through.

---

## Timing sheet

| Pair | Alert | Method | Total time | Of which waiting | Typology you'd file | Agree with copilot? | Sentences you'd change |
|---|---|---|---|---|---|---|---|
| A | 5077454 | by hand | | — | | — | — |
| A | 5077931 | copilot | | | | | |
| B | 5077604 | copilot | | | | | |
| B | 5077725 | by hand | | — | | — | — |

Notes (anything that slowed you down, anything the copilot got wrong):

-

---

## Reporting it

Quote it as: *"On two matched pairs, one analyst (me): X min by hand vs Y min reviewing
the draft."* Always with the caveats above, and with the "sentences you'd change"
column — a faster review of a draft that needed rewriting is not a saving.
