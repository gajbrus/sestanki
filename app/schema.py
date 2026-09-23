"""Pydantic models for the structured LLM output.

All fields are required in the JSON schema (nullable where information may be
missing) so the model must make an explicit choice instead of silently omitting.
"""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Participant(_Strict):
    name: str
    side: Literal["client", "employee", "unknown"]
    employee_id: str | None


class Requirement(_Strict):
    description: str
    priority: Literal["high", "medium", "low", "unknown"]
    evidence: str


class KeyFact(_Strict):
    type: Literal["budget", "deadline", "scope", "technology", "constraint", "other"]
    value: str
    amount: float | None
    currency: str | None
    certainty: Literal["stated", "approximate"]
    evidence: str


class NextStep(_Strict):
    description: str
    owner_employee_id: str | None
    owner_source: Literal["explicit", "suggested_by_domain", "none"]
    due_date: date | None
    evidence: str


class MeetingExtraction(_Strict):
    meeting_date: date | None
    client_company: str | None
    participants: list[Participant]
    summary: str
    client_requirements: list[Requirement]
    key_facts: list[KeyFact]
    next_steps: list[NextStep]
    open_questions: list[str]
    uncertainties: list[str]


def strict_json_schema(model: type[BaseModel] = MeetingExtraction) -> dict:
    """JSON schema suitable for strict structured output (Anthropic / OpenAI):
    every object has additionalProperties=false and lists all properties as required."""
    schema = model.model_json_schema()

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"].keys())
            if isinstance(node.get("title"), str):
                node.pop("title")
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return schema
