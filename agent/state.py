"""Case state carried across the graph. Persisted to Postgres by the
checkpointer, so it survives a worker dying mid-run."""

from typing import TypedDict


class CaseState(TypedDict, total=False):
    case_id: str
    alert_id: int
    evidence: dict          # evidence_id -> fact. Node 1 builds it.
    typology: str
    confidence: float
    reasoning: str
    usage: dict             # token counts per LLM node
