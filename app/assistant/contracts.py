"""Shared assistant API vocabulary; schemas live in docs/openapi/assistant.yaml."""
from __future__ import annotations

from typing import Literal

Scope = Literal["knowledge", "planning"]
PrivacyMode = Literal["local_only", "configured"]
Protocol = Literal["gemini", "ollama", "openai_compatible"]
JobStatus = Literal["queued", "running", "completed", "failed", "cancelled", "expired"]
AnswerStatus = Literal["answered", "needs_clarification", "insufficient_data"]
BasisStatus = Literal["verified_by_scenario", "proposal", "needs_data"]
Freshness = Literal["current", "stale", "superseded"]

INTENTS = frozenset({
    "system_help", "overview", "team_analysis", "task_explanation",
    "metric_explanation", "compare_measures", "changes", "loan_hours", "planning_actions",
})

BASE_PATH = "/api/assistant"
