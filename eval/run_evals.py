"""make eval. No CI integration -- Actions secrets, billing and workflow debugging
are the fiddly part and nobody reads the config. Run it manually.

Measures the claim the project rests on. A project about verification that does
not verify itself fails its own thesis.
"""

import json
import statistics
import time
import uuid
from pathlib import Path

from agent.graph import build, checkpointer_cm
from agent.llm import MODEL

FIXTURES = Path(__file__).parent / "fixtures.json"


def run_one(graph, fixture):
    start = time.time()
    state = graph.invoke(
        {"case_id": str(uuid.uuid4()), "alert_id": fixture["alert_id"]},
        {"configurable": {"thread_id": f"eval-{uuid.uuid4()}"}})
    elapsed = time.time() - start

    narrative = state.get("narrative") or []
    citations = [i for s in narrative for i in s["evidence_ids"]]
    usage = state.get("usage", {})
    return {
        "alert_id": fixture["alert_id"],
        "expected": fixture["typology"],
        "predicted": state.get("typology"),
        "confidence": state.get("confidence"),
        "seconds": elapsed,
        "sentences": len(narrative),
        "citations": len(citations),
        "unresolved": len(state.get("citation_failures") or []),
        "hallucinated": len(state.get("hallucinated_entities") or []),
        "guidance_cites": sum(i.startswith("guidance:") for i in citations),
        "guidance_only": len(state.get("guidance_only") or []),
        "escalated": bool(state.get("escalated")),
        "prompt_tokens": sum(u["prompt_tokens"] for u in usage.values()),
        "completion_tokens": sum(u["completion_tokens"] for u in usage.values()),
        "cached_tokens": sum(u["cached_tokens"] for u in usage.values()),
    }


def main():
    fixtures = json.loads(FIXTURES.read_text())
    with checkpointer_cm() as checkpointer:
        checkpointer.setup()
        graph = build(checkpointer)
        rows = []
        for fixture in fixtures:
            try:
                row = run_one(graph, fixture)
            except Exception as error:   # one bad fixture must not lose the run
                print(f"ERR  {fixture['alert_id']:>8}  {type(error).__name__}: "
                      f"{str(error)[:110]}")
                rows.append({**{k: 0 for k in
                                ("seconds", "sentences", "citations", "unresolved",
                                 "hallucinated", "guidance_cites", "guidance_only",
                                 "prompt_tokens", "completion_tokens",
                                 "cached_tokens")},
                             "alert_id": fixture["alert_id"],
                             "expected": fixture["typology"], "predicted": None,
                             "confidence": None, "escalated": False,
                             "error": f"{type(error).__name__}"})
                continue
            rows.append(row)
            hit = "ok " if row["predicted"] == row["expected"] else "MISS"
            print(f"{hit} {row['alert_id']:>8}  expected {row['expected']:<15} "
                  f"got {str(row['predicted']):<15} {row['seconds']:5.1f}s  "
                  f"{row['citations']:>2} cites, {row['unresolved']} unresolved")

    correct = sum(r["predicted"] == r["expected"] for r in rows)
    cites = sum(r["citations"] for r in rows)
    unresolved = sum(r["unresolved"] for r in rows)
    prompt = sum(r["prompt_tokens"] for r in rows)
    seconds = sorted(r["seconds"] for r in rows)

    print(f"\n## Eval results — {MODEL}, {len(rows)} fixtures\n")
    print("| Metric | Target | Result |")
    print("|---|---|---|")
    errors = sum(1 for r in rows if r.get("error"))
    print(f"| Typology accuracy | report | {correct}/{len(rows)} ({correct/len(rows):.0%}) |")
    print(f"| Citation validity | **100%** | "
          f"{(cites - unresolved) / cites if cites else 1:.1%} ({cites - unresolved}/{cites}) |")
    print(f"| Hallucinated entities | **0** | {sum(r['hallucinated'] for r in rows)} |")
    # Guidance is in `citations` too, so validity above covers it resolving; these
    # say whether the red flags were used, and never used alone (agent/verify.py).
    drafted = [r for r in rows if r["sentences"]]
    print(f"| Narratives citing guidance | report | "
          f"{sum(r['guidance_cites'] > 0 for r in drafted)}/{len(drafted)} "
          f"({sum(r['guidance_cites'] for r in rows)} cites) |")
    print(f"| Guidance-only sentences | **0** | {sum(r['guidance_only'] for r in rows)} |")
    print(f"| Escalated after retry | report | {sum(r['escalated'] for r in rows)} |")
    print(f"| p50 / p95 latency | report | {statistics.median(seconds):.1f}s / "
          f"{seconds[int(len(seconds) * 0.95) - 1]:.1f}s |")
    print(f"| Tokens per case | report | {prompt / len(rows):,.0f} in, "
          f"{sum(r['completion_tokens'] for r in rows) / len(rows):,.0f} out |")
    print(f"| Prompt cache hit rate | report | "
          f"{sum(r['cached_tokens'] for r in rows) / prompt if prompt else 0:.1%} |")
    print(f"| Provider errors | report | {errors} |")

    Path("artifacts/eval_results.json").write_text(json.dumps(rows, indent=2))
    print("\nwrote artifacts/eval_results.json")


if __name__ == "__main__":
    main()
