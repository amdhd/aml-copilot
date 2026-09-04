"""Pydantic schema per LLM node. Every LLM call is schema-validated with one
retry on invalid JSON; a second failure fails the node loudly."""

from enum import Enum

from pydantic import BaseModel, Field


class Typology(str, Enum):
    structuring = "structuring"
    layering = "layering"
    smurfing = "smurfing"
    trade_based = "trade_based"
    rapid_movement = "rapid_movement"
    none = "none"


class TypologyVerdict(BaseModel):
    typology: Typology
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(max_length=600)


class NarrativeSentence(BaseModel):
    """One sentence of the SAR narrative and the evidence it rests on. Sentence
    level, not document level: the verifier resolves each id independently, so a
    single unsupported claim cannot hide inside a well-sourced paragraph."""

    text: str = Field(max_length=400)
    evidence_ids: list[str] = Field(min_length=1)


class Narrative(BaseModel):
    sentences: list[NarrativeSentence] = Field(min_length=2, max_length=12)
