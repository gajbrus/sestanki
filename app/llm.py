"""LLM provider abstraction.

Every provider implements two calls:
  extract_json(system, messages, schema) -> str   # JSON forced to match `schema`
  complete(system, prompt) -> str                  # free text (email polishing only)

`messages` is a plain list of {"role": "user"|"assistant", "content": str}.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Protocol

from app import config


class LLMError(Exception):
    """The provider call failed (API error, refusal, truncated output ...)."""


class LLMProvider(Protocol):
    name: str

    def extract_json(self, system: str, messages: list[dict], schema: dict) -> str: ...

    def complete(self, system: str, prompt: str) -> str: ...


class AnthropicLLM:
    """Claude via structured outputs (output_config.format = json_schema): the API
    constrains decoding to the schema, so the response is always parseable JSON."""

    name = "anthropic"

    def __init__(self, model: str | None = None):
        import anthropic
        self._anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.model = model or config.ANTHROPIC_MODEL

    def _create(self, **kwargs):
        # Current Claude models do not accept `temperature` (see README > Decisions);
        # determinism comes from the schema constraint + strict prompt instead.
        # Server-side fallback: if the primary model declines, the API retries on a fallback model.
        try:
            resp = self.client.beta.messages.create(
                model=self.model, max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
        except self._anthropic.APIError as e:
            raise LLMError(f"Anthropic API error: {e}") from e
        except TypeError as e:  # raised by the SDK when no credentials are configured
            if "authentication" not in str(e):
                raise
            raise LLMError(f"Anthropic credentials missing: {e}") from e
        if resp.stop_reason == "refusal":
            raise LLMError("Model refused the request.")
        if resp.stop_reason == "max_tokens":
            raise LLMError("Model output was truncated (max_tokens).")
        text = "".join(b.text for b in resp.content if b.type == "text")
        if not text:
            raise LLMError("Empty model response.")
        return text

    def extract_json(self, system: str, messages: list[dict], schema: dict) -> str:
        return self._create(system=system, messages=messages,
                            output_config={"format": {"type": "json_schema", "schema": schema}})

    def complete(self, system: str, prompt: str) -> str:
        return self._create(system=system, messages=[{"role": "user", "content": prompt}])


class OpenAILLM:
    """OpenAI via Structured Outputs (response_format json_schema, strict)."""

    name = "openai"

    def __init__(self, model: str | None = None):
        import openai
        self._openai = openai
        try:
            self.client = openai.OpenAI()
        except openai.OpenAIError as e:  # e.g. missing OPENAI_API_KEY
            raise LLMError(f"OpenAI client error: {e}") from e
        self.model = model or config.OPENAI_MODEL

    def _create(self, system: str, messages: list[dict], **kwargs) -> str:
        try:
            resp = self.client.chat.completions.create(
                model=self.model, temperature=config.LLM_TEMPERATURE,
                messages=[{"role": "system", "content": system}, *messages], **kwargs)
        except self._openai.OpenAIError as e:
            raise LLMError(f"OpenAI API error: {e}") from e
        choice = resp.choices[0]
        if getattr(choice.message, "refusal", None):
            raise LLMError(f"Model refused: {choice.message.refusal}")
        if choice.finish_reason == "length":
            raise LLMError("Model output was truncated (length).")
        return choice.message.content or ""

    def extract_json(self, system: str, messages: list[dict], schema: dict) -> str:
        return self._create(system, messages, response_format={
            "type": "json_schema",
            "json_schema": {"name": "meeting_extraction", "strict": True, "schema": schema},
        })

    def complete(self, system: str, prompt: str) -> str:
        return self._create(system, [{"role": "user", "content": prompt}])


class FakeLLM:
    """Deterministic stand-in for tests and key-less demos.

    `responses` is either a list of strings/dicts returned in order (the last one
    repeats), or a callable(messages) -> str|dict.
    """

    name = "fake"

    def __init__(self, responses: list | Callable[[list[dict]], str | dict]):
        self.responses = responses
        self.calls: list[list[dict]] = []

    def extract_json(self, system: str, messages: list[dict], schema: dict) -> str:
        self.calls.append(list(messages))
        if callable(self.responses):
            out = self.responses(messages)
        else:
            out = self.responses[min(len(self.calls), len(self.responses)) - 1]
        if isinstance(out, Exception):
            raise out
        return out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)

    def complete(self, system: str, prompt: str) -> str:
        return prompt

    @classmethod
    def from_fixtures(cls, fixtures_dir: Path) -> "FakeLLM":
        """Pick the fixture whose client_company appears in the transcript header."""
        fixtures = [json.loads(p.read_text(encoding="utf-8"))
                    for p in sorted(fixtures_dir.glob("*.json")) if "hallucinated" not in p.stem]

        def respond(messages: list[dict]) -> dict:
            header = re.search(r"Stranka:\s*(.+)", messages[0]["content"])
            company = header.group(1).strip() if header else ""
            for fx in fixtures:
                if fx["client_company"] and fx["client_company"] in company:
                    return fx
            raise LLMError(f"FakeLLM: no fixture for '{company}'")
        return cls(respond)


def get_llm(provider: str | None = None) -> LLMProvider:
    provider = (provider or config.LLM_PROVIDER).lower()
    if provider == "anthropic":
        return AnthropicLLM()
    if provider == "openai":
        return OpenAILLM()
    if provider == "fake":
        return FakeLLM.from_fixtures(config.ROOT / "tests" / "fixtures")
    raise ValueError(f"Unknown LLM_PROVIDER '{provider}'")
