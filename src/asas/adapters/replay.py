"""Record/replay gateways: capture real model output once, replay it deterministically in
tests and audits (rail 15 verification against a real model's text)."""

from __future__ import annotations

import json
from pathlib import Path

from asas.agents.gateway import ModelGateway, ModelRequest, ModelResponse
from asas.ids import content_hash


def request_key(request: ModelRequest) -> str:
    return content_hash(
        "\x1f".join([request.task, request.prompt_version, request.system, request.payload])
    )


class ReplayGateway:
    def __init__(self, fixtures: dict[str, str], model_id: str = "replay") -> None:
        self._fixtures = fixtures
        self._model_id = model_id

    @classmethod
    def from_file(cls, path: str | Path) -> ReplayGateway:
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def complete(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(self._fixtures[request_key(request)], self._model_id)


class RecordingGateway:
    def __init__(self, inner: ModelGateway) -> None:
        self._inner = inner
        self.recorded: dict[str, str] = {}

    def complete(self, request: ModelRequest) -> ModelResponse:
        response = self._inner.complete(request)
        self.recorded[request_key(request)] = response.text
        return response

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.recorded, sort_keys=True, indent=1), encoding="utf-8")
