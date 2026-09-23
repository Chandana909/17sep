"""Rail 4: agent prose may not contain numbers outside `{{F<n>}}` placeholders."""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence

from asas.report import PLACEHOLDER

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"^```[a-zA-Z]*\n?|\n?```$")


def clean_model_text(text: str) -> str:
    """Strip reasoning blocks and markdown fences that small models (e.g. Qwen) often emit.
    Cleaning never adds content; validation still runs on the result."""
    text = _THINK.sub("", text).strip()
    text = _FENCE.sub("", text).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text


def validate_prose(
    text: str, fact_ids: Collection[str], whitelist: Sequence[str]
) -> tuple[str, ...]:
    errors: list[str] = []
    for fid in PLACEHOLDER.findall(text):
        if fid not in fact_ids:
            errors.append(f"UNKNOWN_FACT:{fid}")
    stripped = PLACEHOLDER.sub(" ", text)
    for token in sorted(whitelist, key=len, reverse=True):
        stripped = stripped.replace(token, " ")
    if any(ch.isnumeric() for ch in stripped):
        errors.append("DIGIT_OUTSIDE_PLACEHOLDER")
    if "{{" in stripped or "}}" in stripped:
        errors.append("MALFORMED_PLACEHOLDER")
    if not text.strip():
        errors.append("EMPTY_OUTPUT")
    return tuple(errors)
