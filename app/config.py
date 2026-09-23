"""Configuration from environment variables (.env supported)."""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _env(name: str, default: str) -> str:
    return os.getenv(name) or default


def _path(name: str, default: str) -> Path:
    """Relative paths in .env are resolved against the project root."""
    path = Path(_env(name, default))
    return path if path.is_absolute() else ROOT / path


LLM_PROVIDER = _env("LLM_PROVIDER", "anthropic")          # anthropic | openai | fake
ANTHROPIC_MODEL = _env("ANTHROPIC_MODEL", "claude-opus-5")
OPENAI_MODEL = _env("OPENAI_MODEL", "gpt-4.1")
LLM_TEMPERATURE = float(_env("LLM_TEMPERATURE", "0"))
LLM_POLISH_EMAIL = _env("LLM_POLISH_EMAIL", "false").lower() == "true"

CRM_BASE_URL = _env("CRM_BASE_URL", "http://127.0.0.1:8001")
CRM_TIMEOUT_SECONDS = float(_env("CRM_TIMEOUT_SECONDS", "5"))

DB_PATH = _path("DB_PATH", "meeting_ai.db")
TRANSCRIPTS_DIR = _path("TRANSCRIPTS_DIR", "data/transcripts")
EMPLOYEES_PATH = _path("EMPLOYEES_PATH", "data/employees.json")
OUTPUTS_DIR = _path("OUTPUTS_DIR", "outputs")

COMPANY_NAME = _env("COMPANY_NAME", "Svetovanje Plus d.o.o.")
# Simulated login for the review UI (production: identity from SSO)
REVIEWER_EMPLOYEE_ID = _env("REVIEWER_EMPLOYEE_ID", "E001")


@lru_cache
def load_employees(path: str | None = None) -> list[dict]:
    with open(path or EMPLOYEES_PATH, encoding="utf-8") as f:
        return json.load(f)


def employees_by_id() -> dict[str, dict]:
    return {e["id"]: e for e in load_employees()}
