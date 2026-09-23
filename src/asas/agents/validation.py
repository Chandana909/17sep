"""Rail 4: agent prose may not contain numbers outside `{{F<n>}}` placeholders."""

from __future__ import annotations

from collections.abc import Collection, Sequence

from asas.report import PLACEHOLDER


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
