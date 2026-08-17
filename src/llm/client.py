"""Minimal client for the local llama.cpp OpenAI-compatible server.

Local-first: intended for 127.0.0.1 only. No cloud SDK, plain httpx.

The server is expected to run in *router mode* (``llama-server
--models-preset config/llama-models.ini``): the ``model`` field of each
request names a preset section and the router loads that model on demand,
swapping out the previous one when ``--models-max`` is reached. A classic
single-model server works too — it simply ignores the ``model`` field.
"""

from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)


class LLMError(Exception):
    """Raised when the local LLM server cannot produce a completion."""


class LLMClient:
    def __init__(self, *, base_url: str = "http://127.0.0.1:8080",
                 model: str = "muse-glimmer-30b", timeout_seconds: float = 600,
                 max_retries: int = 2, reasoning_effort: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout_seconds
        self.max_retries = max_retries
        self.reasoning_effort = reasoning_effort

    def with_model(self, model: str | None) -> "LLMClient":
        """Same server and limits, different model (per pipeline stage)."""
        if not model or model == self.model:
            return self
        return LLMClient(base_url=self.base_url, model=model,
                         timeout_seconds=self.timeout, max_retries=self.max_retries,
                         reasoning_effort=self.reasoning_effort)

    def is_alive(self) -> bool:
        """Check whether the llama.cpp server is up (GET /health)."""
        for path in ("/health", "/v1/models"):
            try:
                resp = httpx.get(f"{self.base_url}{path}", timeout=5)
                if resp.status_code == 200:
                    return True
            except httpx.HTTPError:
                continue
        return False

    def context_size(self) -> int | None:
        """Ask llama.cpp for the model's context window (GET /props).

        In router mode /props needs ``?model=`` and loading the model on
        first touch can take a minute, hence the generous timeout. None if
        unknown."""
        try:
            resp = httpx.get(f"{self.base_url}/props",
                             params={"model": self.model}, timeout=300)
            if resp.status_code == 200:
                data = resp.json()
                n_ctx = (data.get("default_generation_settings") or {}).get("n_ctx") \
                    or data.get("n_ctx")
                return int(n_ctx) if n_ctx else None
        except (httpx.HTTPError, ValueError, TypeError):
            pass
        return None

    def chat(self, messages: list[dict], *, temperature: float = 0.4,
             max_tokens: int = 3000) -> str:
        """POST /v1/chat/completions with retries. Raises LLMError on failure."""
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if self.reasoning_effort:
            # Reasoning models burn output tokens on thinking before they
            # answer. llama.cpp forwards this for templates that support it
            # and ignores it otherwise, so sending it is always safe.
            payload["reasoning_effort"] = self.reasoning_effort
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 2):
            try:
                log.info("LLM call attempt %d/%d (model=%s)",
                         attempt, self.max_retries + 1, self.model)
                resp = httpx.post(
                    f"{self.base_url}/v1/chat/completions",
                    json=payload,
                    timeout=self.timeout,
                )
                if resp.status_code >= 500:
                    raise LLMError(f"server error HTTP {resp.status_code}: {resp.text[:300]}")
                if resp.status_code != 200:
                    # 4xx will not get better on retry — fail immediately.
                    raise LLMError(
                        f"request rejected HTTP {resp.status_code}: {resp.text[:300]}"
                    ) from None
                data = resp.json()
                choice = data["choices"][0]
                message = choice.get("message", {})
                content = message.get("content")
                finish = choice.get("finish_reason")
                if not isinstance(content, str) or not content.strip():
                    if message.get("reasoning_content") and finish == "length":
                        raise LLMError(
                            "model spent the whole max_tokens budget on reasoning "
                            "and produced no answer — increase llm.max_tokens in "
                            "config/settings.yaml"
                        ) from None
                    raise LLMError("empty completion returned")
                if finish == "length":
                    log.warning("Completion hit max_tokens — the brief may be truncated")
                return content
            except httpx.ConnectError as exc:
                raise LLMError(
                    f"cannot connect to LLM server at {self.base_url} — is llama-server "
                    f"running? ({exc})"
                ) from exc
            except httpx.TimeoutException as exc:
                last_error = exc
                log.warning(
                    "LLM call attempt %d timed out after %.0fs — a 30B model can be "
                    "slow; consider raising llm.timeout_seconds", attempt, self.timeout)
            except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
                last_error = exc
                log.warning("LLM call attempt %d failed: %s", attempt, exc)
            except LLMError as exc:
                if "server error" not in str(exc):
                    raise
                last_error = exc
                log.warning("LLM call attempt %d failed: %s", attempt, exc)
            if attempt <= self.max_retries:
                time.sleep(2 * attempt)
        raise LLMError(f"LLM call failed after {self.max_retries + 1} attempts: {last_error}")
