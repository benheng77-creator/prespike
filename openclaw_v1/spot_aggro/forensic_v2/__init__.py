"""
SPOT AGGRO 5-LLM forensic-truth system (v2).

Truth-first, evidence-ranked forensic reports per the principal spec.
Strict separation from any apex_omega / perp logic.
"""

from .orchestrator import generate_report, get_report, list_reports

__all__ = ["generate_report", "get_report", "list_reports"]
