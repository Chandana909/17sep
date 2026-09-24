"""Output validation for model text. Structure is validated by pydantic action schemas; prose
is validated here: no invented numbers, only citations of evidence that exists."""

from __future__ import annotations

import re
from collections.abc import Collection

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")
_CITATION = re.compile(r"\[(E\d+)\]")


def clean_model_text(text: str) -> str:
    """Strip reasoning blocks and markdown fences that small models (e.g. Qwen) emit. Never
    adds content; validation still runs on the result."""
    text = _THINK.sub("", text).strip()
    text = _FENCE.sub("", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        text = text[start : end + 1]
    return text


def validate_prose(text: str, evidence_ids: Collection[str]) -> list[str]:
    errors: list[str] = []
    for cited in _CITATION.findall(text):
        if cited not in evidence_ids:
            errors.append(f"UNKNOWN_EVIDENCE:{cited}")
    stripped = _CITATION.sub(" ", text)
    if any(ch.isnumeric() for ch in stripped):
        errors.append("NUMBER_OUTSIDE_CITATION")
    if len(text) > 2000:
        errors.append("TOO_LONG")
    return errors


def cited(text: str) -> list[str]:
    return _CITATION.findall(text)
