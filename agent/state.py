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
    narrative: list         # [{text, evidence_ids}] from node 4
    draft_attempts: int     # node 4 runs at most twice
    citation_failures: list # evidence_ids that do not resolve
    hallucinated_entities: list
    verified: bool
    escalated: bool         # failed verification twice
    decision: str           # human_review outcome
    usage: dict             # token counts per LLM node
