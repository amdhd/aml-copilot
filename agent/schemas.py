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
