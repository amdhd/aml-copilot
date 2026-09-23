"""Node 3: pull regulatory guidance for this typology. No LLM.

Retrieved chunks enter the evidence bundle with their own ids, so the narrative
can cite guidance the same way it cites transactions, and the week 5 verifier
resolves both through one mechanism.
"""

import psycopg
from pgvector.psycopg import register_vector
from sentence_transformers import SentenceTransformer

from agent.nodes.classify_typology import DEFINITIONS
from db import DSN
from rag.ingest import MODEL

TOP_K = 4
_model = None


def _embedder():
    global _model
    if _model is None:
        _model = SentenceTransformer(MODEL)
    return _model


def retrieve_guidance(state: dict) -> dict:
    # Red flags for the typology, not report-writing advice. The query used to
    # end "what must a SAR narrative describe?", which pulled FinCEN's writing
    # guide into 2-3 of the 4 slots for every typology once red-flag documents
    # were in the corpus. The payment format stays: FFIEC lists red flags by it.
    alert = next(v for v in state["evidence"].values() if v["kind"] == "alert")
    typology = state["typology"]
    query = (f"Red flags and indicators of {typology.replace('_', ' ')} money "
             f"laundering: {DEFINITIONS[typology]}. Payment by "
             f"{alert['payment_format']}.")

    vector = _embedder().encode([query], prompt_name="query",
                                show_progress_bar=False)[0]
    with psycopg.connect(DSN) as conn:
        register_vector(conn)
        rows = conn.execute(
            "SELECT source, chunk_ix, text, 1 - (embedding <=> %s) FROM guidance "
            "ORDER BY embedding <=> %s LIMIT %s", (vector, vector, TOP_K)).fetchall()

    guidance = {f"guidance:{r[0]}#{r[1]}": {
        "kind": "guidance", "source": r[0], "chunk": r[1],
        "similarity": round(float(r[3]), 3), "text": r[2]} for r in rows}
    return {"evidence": {**state["evidence"], **guidance}}
