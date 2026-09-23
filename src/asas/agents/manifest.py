"""Decision-environment manifest per agent invocation (specs/agents.md section 7)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DecisionManifest:
    invocation_id: str
    task: str
    subject_id: str
    model_id: str
    prompt_version: str
    config_version: str
    field_contract_version: str
    as_of: str
    input_hash: str
    output_hash: str | None
    tools: tuple[str, ...]
    fact_ids: tuple[str, ...]
    validation_errors: tuple[str, ...]
    fallback_used: bool


class ManifestLog:
    """Append-only."""

    def __init__(self) -> None:
        self._entries: list[DecisionManifest] = []

    def append(self, manifest: DecisionManifest) -> None:
        self._entries.append(manifest)

    @property
    def entries(self) -> tuple[DecisionManifest, ...]:
        return tuple(self._entries)
