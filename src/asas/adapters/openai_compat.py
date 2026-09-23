"""ModelGateway for any OpenAI-compatible chat endpoint: Qwen via Ollama / vLLM / LM Studio /
DashScope, or hosted models. Holds only a model API key, never DB credentials (rail 13).
Weak models are safe here: output is validated and falls back to templates."""

from __future__ import annotations

import json
import os
import urllib.request
from collections.abc import Callable
from typing import Any

from asas.agents.gateway import ModelRequest, ModelResponse

Opener = Callable[[urllib.request.Request, float], Any]


def _default_opener(req: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.urlopen(req, timeout=timeout)


class OpenAICompatibleGateway:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key_env: str | None = None,
        timeout_seconds: float = 60,
        max_tokens: int = 800,
        opener: Opener = _default_opener,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must be http(s)")
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._model = model
        self._key = os.environ.get(api_key_env, "") if api_key_env else ""
        self._timeout = timeout_seconds
        self._max_tokens = max_tokens
        self._opener = opener

    def complete(self, request: ModelRequest) -> ModelResponse:
        body = {
            "model": self._model,
            "temperature": 0,
            "max_tokens": self._max_tokens,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.payload},
            ],
        }
        headers = {"Content-Type": "application/json"}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        req = urllib.request.Request(
            self._url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
        )
        with self._opener(req, self._timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = data["choices"][0]["message"]["content"]
        if not isinstance(text, str):
            raise ValueError("model returned non-text content")
        return ModelResponse(text, str(data.get("model", self._model)))
