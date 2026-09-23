"""The only path to an LLM. Production adapters implement ModelGateway (docs/SAD.md)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ModelRequest:
    task: str
    model_id: str
    prompt_version: str
    system: str
    payload: str
    tools: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ModelResponse:
    text: str
    model_id: str


class ModelGateway(Protocol):
    def complete(self, request: ModelRequest) -> ModelResponse: ...


@dataclass
class FakeModelGateway:
    """Deterministic, fixture-driven: returns `responses[task]`; raises if absent."""

    responses: Mapping[str, str]
    model_id: str = "fake-model"
    calls: list[ModelRequest] = field(default_factory=list)

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls.append(request)
        return ModelResponse(self.responses[request.task], self.model_id)
