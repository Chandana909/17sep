"""Model gateway: the only path to an LLM.

`OpenAICompatibleGateway` talks to any /chat/completions endpoint (Qwen via Ollama or vLLM,
LM Studio, DashScope, hosted models). `ResilientGateway` adds retries with backoff and a
circuit breaker; `CachingGateway` persists responses by request hash so a run can be replayed
from its record. `ScriptedGateway` is the deterministic test double.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from asas.core.errors import ModelError
from asas.core.ids import canonical_json, content_hash
from asas.store.db import Store, ts_key, utcnow


class ModelRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    model_id: str
    prompt_id: str
    prompt_version: str
    system: str
    user: str
    max_tokens: int

    @property
    def request_hash(self) -> str:
        return content_hash(canonical_json(self.model_dump()))


class ModelResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    text: str
    model_id: str
    cached: bool = False


class ModelGateway(Protocol):
    model_id: str

    def complete(self, request: ModelRequest) -> ModelResponse: ...


Opener = Callable[[urllib.request.Request, float], Any]


def _open(req: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.urlopen(req, timeout=timeout)


class OpenAICompatibleGateway:
    def __init__(
        self,
        base_url: str,
        model_id: str,
        api_key_env: str | None,
        timeout_seconds: float,
        opener: Opener = _open,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must be http(s)")
        self.model_id = model_id
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._key = os.environ.get(api_key_env, "") if api_key_env else ""
        self._timeout = timeout_seconds
        self._opener = opener

    def complete(self, request: ModelRequest) -> ModelResponse:
        body = {
            "model": self.model_id,
            "temperature": 0,
            "max_tokens": request.max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
        }
        headers = {"Content-Type": "application/json"}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        req = urllib.request.Request(
            self._url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
        )
        try:
            with self._opener(req, self._timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise ModelError(f"model transport failed: {type(exc).__name__}") from exc
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelError("model returned an unexpected payload") from exc
        if not isinstance(text, str):
            raise ModelError("model returned non-text content")
        return ModelResponse(text=text, model_id=str(data.get("model", self.model_id)))


class ScriptedGateway:
    """Deterministic stand-in for an LLM: a function from request to response text."""

    def __init__(self, script: Callable[[ModelRequest], str], model_id: str = "scripted") -> None:
        self.model_id = model_id
        self._script = script
        self.calls: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls.append(request)
        return ModelResponse(text=self._script(request), model_id=self.model_id)


class CircuitBreaker:
    def __init__(self, threshold: int, reset_seconds: float) -> None:
        self._threshold = threshold
        self._reset = reset_seconds
        self._failures = 0
        self._opened_at: float | None = None
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            if self._opened_at is None:
                return True
            if time.monotonic() - self._opened_at >= self._reset:
                self._opened_at = None  # half-open: let one call through
                self._failures = self._threshold - 1
                return True
            return False

    def record(self, ok: bool) -> None:
        with self._lock:
            if ok:
                self._failures = 0
                self._opened_at = None
                return
            self._failures += 1
            if self._failures >= self._threshold:
                self._opened_at = time.monotonic()

    @property
    def is_open(self) -> bool:
        return self._opened_at is not None


class ResilientGateway:
    def __init__(
        self,
        inner: ModelGateway,
        retries: int,
        backoff_seconds: float,
        breaker: CircuitBreaker,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model_id = inner.model_id
        self._inner = inner
        self._retries = retries
        self._backoff = backoff_seconds
        self.breaker = breaker
        self._sleep = sleep

    def complete(self, request: ModelRequest) -> ModelResponse:
        if not self.breaker.allow():
            raise ModelError("circuit open")
        last: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                response = self._inner.complete(request)
                self.breaker.record(True)
                return response
            except ModelError as exc:
                last = exc
                self.breaker.record(False)
                if self.breaker.is_open:
                    break
                if attempt < self._retries:
                    self._sleep(self._backoff * (2**attempt))
        raise ModelError(f"model unavailable after retries: {last}")


class CachingGateway:
    """Replay-by-record: identical requests return the recorded response."""

    def __init__(self, inner: ModelGateway, store: Store) -> None:
        self.model_id = inner.model_id
        self._inner = inner
        self._store = store

    def complete(self, request: ModelRequest) -> ModelResponse:
        rows = self._store.query(
            "SELECT response, model_id FROM model_cache WHERE request_hash = ?",
            (request.request_hash,),
        )
        if rows:
            return ModelResponse(text=str(rows[0][0]), model_id=str(rows[0][1]), cached=True)
        response = self._inner.complete(request)
        self._store.insert_ignore(
            "model_cache",
            {
                "request_hash": request.request_hash,
                "model_id": response.model_id,
                "response": response.text,
                "created_at": ts_key(utcnow()),
            },
        )
        return response
