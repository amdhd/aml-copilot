"""Tiny CSV in the IBM AML schema, so the pipeline can be run without the real
download. Numbers produced from this file are not results."""

from pathlib import Path

import numpy as np
import pandas as pd

RNG = np.random.default_rng(0)
FORMATS = ["ACH", "Cheque", "Credit Card", "Wire", "Cash", "Reinvestment"]
CURRENCIES = ["US Dollar", "Euro", "Yuan"]
START = pd.Timestamp("2022-09-01")
N_ACCOUNTS, N_BACKGROUND, N_RINGS = 400, 8000, 60


def account(i):
    return f"{RNG.integers(10, 30)}", f"{i:08X}"


rows = []
accounts = [account(i) for i in range(N_ACCOUNTS)]


def add(t, a, b, amount, launder):
    cur = CURRENCIES[RNG.integers(len(CURRENCIES))]
    rows.append([t.strftime("%Y/%m/%d %H:%M"), a[0], a[1], b[0], b[1],
                 round(amount, 2), cur, round(amount, 2), cur,
                 FORMATS[RNG.integers(len(FORMATS))], launder])


for _ in range(N_BACKGROUND):
    i, j = RNG.integers(N_ACCOUNTS, size=2)
    add(START + pd.Timedelta(minutes=int(RNG.integers(43200))),
        accounts[i], accounts[j], RNG.lognormal(7, 1.2), 0)

# Laundering: one lump in, then split out into small pieces within a few hours.
for _ in range(N_RINGS):
    mule = accounts[RNG.integers(N_ACCOUNTS)]
    t0 = START + pd.Timedelta(minutes=int(RNG.integers(40000)))
    lump = RNG.uniform(50_000, 200_000)
    add(t0, accounts[RNG.integers(N_ACCOUNTS)], mule, lump, 1)
    n_out = RNG.integers(4, 9)
    for k in range(n_out):
        add(t0 + pd.Timedelta(minutes=int(20 * (k + 1))), mule,
            accounts[RNG.integers(N_ACCOUNTS)], lump / n_out, 1)

df = pd.DataFrame(rows).sort_values(0)
header = ("Timestamp,From Bank,Account,To Bank,Account,Amount Received,"
          "Receiving Currency,Amount Paid,Payment Currency,Payment Format,"
          "Is Laundering")
# data/ is gitignored, so it does not exist in a fresh clone.
Path("data").mkdir(exist_ok=True)
with open("data/smoke_Trans.csv", "w") as f:
    f.write(header + "\n")
    df.to_csv(f, header=False, index=False)
print(f"{len(df)} rows, {df[10].mean():.3%} illicit -> data/smoke_Trans.csv")
