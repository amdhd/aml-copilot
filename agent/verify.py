"""Node 5: citation verification. Deterministic Python, no LLM.

An LLM judging another LLM's citations is unfalsifiable and would not survive a
compliance review. Every evidence_id in the draft must resolve to a fact that
node 1 actually assembled, and every account named in prose must be one that
appears in the bundle. This node is the point of the project.

Guidance can support a sentence but not carry it alone. A red flag from FATF
or FFIEC says what a pattern looks like in general; a sentence resting only on
one makes a claim about this case with nothing from this case behind it, and
every id in it would still resolve. The same goes for a sentence resting only
on the model's attention weights: those say which transactions the GNN looked
at, not what happened in them.

Resolving is not supporting. An id that exists says nothing about whether the
fact behind it states what the sentence does, so every account, amount and
date a sentence names must appear in the facts that sentence cites -- not
merely somewhere in the bundle.
"""

import re

# Accounts in this dataset are "<bank>-<hex account>", e.g. 14766-805C42580.
# Anything of that shape appearing in narrative prose is a claim about an entity.
ACCOUNT = re.compile(r"\b\d{1,6}-[0-9A-Fa-f]{6,12}\b")
# The same account with its bank prefix left off, which ACCOUNT alone let
# through unchecked. At least one digit and one letter, so neither a plain
# number nor an English word is taken for one.
BARE_ACCOUNT = re.compile(
    r"\b(?=[0-9A-Fa-f]*\d)(?=[0-9A-Fa-f]*[A-Fa-f])[0-9A-Fa-f]{8,12}\b")
# Money-shaped: thousands separators or a decimal point. A bare integer is left
# alone -- "3 transfers" is a count the facts never state as a field.
AMOUNT = re.compile(r"(?<![\w.])(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+)(?![\w-])")
DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
CASE_FACTS = {"alert", "transaction"}
MAX_ATTEMPTS = 2


def _stated_by(facts: list[dict]) -> tuple[set, list, set]:
    """The accounts, numbers and dates a set of facts actually states. Numbers
    and dates inside text count too: a guidance chunk that names a $10,000
    threshold supports a sentence that repeats it."""
    accounts, numbers, dates = set(), [], set()
    for fact in facts:
        for key, value in fact.items():
            if key in ("src_account", "dst_account"):
                accounts.add(value.upper())
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                numbers.append(float(value))
            elif isinstance(value, str):
                numbers += [float(n.replace(",", "")) for n in AMOUNT.findall(value)]
                dates.update(DATE.findall(value))
    return accounts, numbers, dates


def _named_accounts(text: str) -> list[str]:
    """Every account the prose names, with or without its bank prefix."""
    prefixed = ACCOUNT.findall(text)
    return prefixed + BARE_ACCOUNT.findall(ACCOUNT.sub(" ", text))


def _holds(accounts: set, account: str) -> bool:
    account = account.upper()
    if "-" in account:
        return account in accounts
    return any(a.split("-", 1)[1] == account for a in accounts)


def _matches(written: str, numbers: list) -> bool:
    """A number written to k decimals matches a fact rounded to k decimals, so
    408,253.83 may be written 408,254 but not 408,000."""
    places = len(written.partition(".")[2])
    value = float(written.replace(",", ""))
    return any(f"{n:.{places}f}" == f"{value:.{places}f}" for n in numbers)


def verify_citations(state: dict) -> dict:
    evidence = state["evidence"]
    known = {fact[key].upper() for fact in evidence.values()
             for key in ("src_account", "dst_account") if key in fact}

    unresolved, hallucinated, guidance_only = [], [], []
    no_case_fact, unsupported = [], []
    for index, sentence in enumerate(state["narrative"]):
        for evidence_id in sentence["evidence_ids"]:
            if evidence_id not in evidence:
                unresolved.append({"sentence": index, "evidence_id": evidence_id})
        cited = [evidence[i] for i in sentence["evidence_ids"] if i in evidence]
        kinds = {fact["kind"] for fact in cited}
        if kinds == {"guidance"}:
            guidance_only.append({"sentence": index})
        elif kinds and not kinds & CASE_FACTS:
            no_case_fact.append({"sentence": index})

        accounts, numbers, dates = _stated_by(cited)
        text = sentence["text"]
        for account in _named_accounts(text):
            if not _holds(known, account):
                hallucinated.append({"sentence": index, "account": account})
            elif not _holds(accounts, account):
                unsupported.append({"sentence": index, "kind": "account", "value": account})
        for amount in AMOUNT.findall(text):
            if not _matches(amount, numbers):
                unsupported.append({"sentence": index, "kind": "amount", "value": amount})
        for date in DATE.findall(text):
            if date not in dates:
                unsupported.append({"sentence": index, "kind": "date", "value": date})

    attempts = state.get("draft_attempts", 1)
    passed = not (unresolved or hallucinated or guidance_only
                  or no_case_fact or unsupported)
    return {
        "citation_failures": unresolved,
        "hallucinated_entities": hallucinated,
        "guidance_only": guidance_only,
        "no_case_fact": no_case_fact,
        "unsupported": unsupported,
        "verified": passed,
        # One retry, then escalate flagged. Never silently ship an unverified draft.
        "escalated": not passed and attempts >= MAX_ATTEMPTS,
    }
