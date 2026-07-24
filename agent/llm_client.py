"""
llm_client.py — Thin wrapper around httpx for talking to any OpenAI-compatible
chat-completions endpoint (vLLM, Ollama, LM Studio, OpenAI itself, etc).

This module has ZERO knowledge of the harness's policy/executor internals —
it only knows how to speak the OpenAI /v1/chat/completions dialect. Routing,
failover, and role-selection live in llm_router.py.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Iterator

import httpx

log = logging.getLogger("harness.llm_client")

DEFAULT_TIMEOUT = 120.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE = 0.5  # seconds; exponential: base * 2**attempt


class LlmClientError(Exception):
    """Raised when the backend cannot be reached after retries, or returns
    a non-recoverable error."""


class OpenAICompatibleClient:
    """
    A minimal client for any server that implements the OpenAI
    /v1/chat/completions and /v1/models surface (vLLM, Ollama's OpenAI
    shim, LM Studio, TGI, OpenAI proper, etc).

    Parameters
    ----------
    base_url:
        e.g. "http://eran1.local:8000/v1" (no trailing slash required).
    model:
        model name/id as the backend expects it in the "model" field,
        e.g. "glm-4.6", "qwen3-coder-480b", "deepseek-v3.1".
    api_key:
        optional bearer token. vLLM/Ollama usually don't require one;
        left blank by default.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base

    # -----------------------------------------------------------------------
    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _client(self, timeout: float | None = None) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, timeout=timeout or self.timeout, headers=self._headers())

    # -----------------------------------------------------------------------
    def _retrying_post(self, path: str, json_body: dict, stream: bool = False):
        """
        Shared retry/backoff logic for POST requests. Retries only on
        connection-level errors (ConnectError, ConnectTimeout, ReadTimeout,
        network flakiness) — not on 4xx/5xx HTTP responses, which are
        surfaced immediately since retrying them rarely helps and can hide
        real errors (e.g. bad model name).
        """
        attempt = 0
        last_exc: Exception | None = None
        while attempt < self.max_retries:
            try:
                client = self._client()
                if stream:
                    # Caller manages the streaming context; just hand back
                    # a context manager for `with`.
                    return client, client.stream("POST", path, json=json_body)
                resp = client.post(path, json=json_body)
                resp.raise_for_status()
                return client, resp
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
                last_exc = e
                attempt += 1
                if attempt >= self.max_retries:
                    break
                sleep_for = self.backoff_base * (2 ** (attempt - 1))
                log.warning(
                    f"[{self.base_url}] connection error (attempt {attempt}/{self.max_retries}): {e}; "
                    f"retrying in {sleep_for:.1f}s"
                )
                time.sleep(sleep_for)
            except httpx.HTTPStatusError as e:
                # Non-retryable — surface immediately with useful detail.
                body = ""
                try:
                    body = e.response.text[:2000]
                except Exception:
                    pass
                raise LlmClientError(
                    f"HTTP {e.response.status_code} from {self.base_url}{path}: {body}"
                ) from e
        raise LlmClientError(
            f"Failed to reach {self.base_url}{path} after {self.max_retries} attempts: {last_exc}"
        ) from last_exc

    # -----------------------------------------------------------------------
    def chat_completion(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        **extra: Any,
    ) -> dict:
        """
        Blocking, non-streaming chat completion. Returns the parsed JSON
        response body (OpenAI schema): {"choices": [{"message": {...}}], ...}
        """
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if tools:
            body["tools"] = tools
        body.update(extra)

        client, resp = self._retrying_post("/chat/completions", body, stream=False)
        try:
            return resp.json()
        finally:
            client.close()

    # -----------------------------------------------------------------------
    def stream_completion(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        **extra: Any,
    ) -> Iterator[str]:
        """
        Streaming chat completion. Yields text deltas (content chunks) as
        they arrive, following the OpenAI SSE `data: {...}` framing that
        vLLM/Ollama also implement. Ends when `data: [DONE]` is seen.

        Retries (per the same policy as chat_completion) only apply to the
        initial connection attempt — once streaming has started, a mid-stream
        drop is raised to the caller rather than silently retried, since
        partial output may already have been consumed.
        """
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if tools:
            body["tools"] = tools
        body.update(extra)

        attempt = 0
        last_exc: Exception | None = None
        while attempt < self.max_retries:
            try:
                with self._client() as client:
                    with client.stream("POST", "/chat/completions", json=body) as resp:
                        resp.raise_for_status()
                        for line in resp.iter_lines():
                            if not line:
                                continue
                            if line.startswith("data:"):
                                data_str = line[len("data:"):].strip()
                            else:
                                data_str = line.strip()
                            if data_str == "[DONE]":
                                return
                            try:
                                chunk = json.loads(data_str)
                            except json.JSONDecodeError:
                                continue
                            for choice in chunk.get("choices", []):
                                delta = choice.get("delta", {})
                                content = delta.get("content")
                                if content:
                                    yield content
                return
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
                last_exc = e
                attempt += 1
                if attempt >= self.max_retries:
                    raise LlmClientError(
                        f"Failed to stream from {self.base_url}/chat/completions after "
                        f"{self.max_retries} attempts: {last_exc}"
                    ) from last_exc
                sleep_for = self.backoff_base * (2 ** (attempt - 1))
                log.warning(
                    f"[{self.base_url}] stream connection error (attempt {attempt}/{self.max_retries}): {e}; "
                    f"retrying in {sleep_for:.1f}s"
                )
                time.sleep(sleep_for)
            except httpx.HTTPStatusError as e:
                body_text = ""
                try:
                    body_text = e.response.text[:2000]
                except Exception:
                    pass
                raise LlmClientError(
                    f"HTTP {e.response.status_code} from {self.base_url}/chat/completions: {body_text}"
                ) from e

    # -----------------------------------------------------------------------
    def list_models(self) -> dict:
        """GET /v1/models — used for health checks."""
        with self._client(timeout=10.0) as client:
            resp = client.get("/models")
            resp.raise_for_status()
            return resp.json()

    # -----------------------------------------------------------------------
    def health_check(self, timeout: float = 10.0) -> bool:
        """
        Lightweight liveness probe used by LlmRouter's background thread.
        Returns True iff the backend responds to GET /v1/models within
        `timeout` seconds with a 2xx.
        """
        try:
            with self._client(timeout=timeout) as client:
                resp = client.get("/models")
                return resp.status_code < 400
        except Exception as e:
            log.debug(f"[{self.base_url}] health check failed: {e}")
            return False

    def __repr__(self):
        return f"OpenAICompatibleClient(base_url={self.base_url!r}, model={self.model!r})"
