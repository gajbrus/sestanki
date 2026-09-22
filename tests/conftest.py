import json
from pathlib import Path

import pytest

from app.schema import MeetingExtraction

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
TRANSCRIPTS = ROOT / "data" / "transcripts"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def load_extraction(name: str) -> MeetingExtraction:
    return MeetingExtraction.model_validate(load_fixture(name))


def load_transcript(name: str) -> str:
    return (TRANSCRIPTS / f"{name}.txt").read_text(encoding="utf-8")


@pytest.fixture
def normal():
    return load_extraction("normal"), load_transcript("normal")
