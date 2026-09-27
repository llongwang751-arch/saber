"""Durable, evidence-driven research workers independent of HTTP and scheduling."""

from .engine import ResearchEngine, ResearchLimits, ResearchResult
from .sources import SourceLedger
from .reporting import validate_citations

__all__ = ["ResearchEngine", "ResearchLimits", "ResearchResult", "SourceLedger", "validate_citations"]
