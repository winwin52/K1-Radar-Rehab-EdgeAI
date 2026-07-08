"""
DeepSeek LLM client — OpenAI-compatible API.

DeepSeek uses the same /v1/chat/completions schema as OpenAI; the only
difference is base_url and model names. This client also works for any
OpenAI-compatible endpoint (Qwen DashScope's compat mode, Moonshot, etc.)
by changing base_url + model.
"""

from __future__ import annotations

import json
import time

import httpx

from .client_base import (ChatMessage, ChatResult, LLMClient,
                          LLMError, LLMFatalError, LLMRetryableError)


DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL    = "deepseek-chat"
DEFAULT_TIMEOUT  = 30.0


class DeepSeekClient(LLMClient):
    def __init__(self, api_key: str,
                 base_url: str = DEFAULT_BASE_URL,
                 default_model: str = DEFAULT_MODEL,
                 timeout_s: float = DEFAULT_TIMEOUT):
        if not api_key:
            raise ValueError("DeepSeek requires a non-empty api_key")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.default_model = default_model
        self.default_timeout_s = timeout_s

    async def chat(self, messages: list[ChatMessage], *,
                   model: str | None = None,
                   temperature: float | None = None,
                   max_tokens: int | None = None,
                   timeout_s: float | None = None) -> ChatResult:
        url = f"{self.base_url}/chat/completions"
        payload: dict = {
            "model": model or self.default_model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": False,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type":  "application/json",
        }

        t_start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=timeout_s or self.default_timeout_s) as client:
                resp = await client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as e:
            raise LLMRetryableError(f"timeout after {timeout_s or self.default_timeout_s}s") from e
        except httpx.NetworkError as e:
            raise LLMRetryableError(f"network error: {e}") from e
        except Exception as e:
            raise LLMError(f"unexpected http error: {e}") from e

        latency_ms = (time.monotonic() - t_start) * 1000

        code = resp.status_code
        # Status code routing
        if code == 401:
            raise LLMFatalError("invalid API key (401)")
        if code in (402, 403):
            raise LLMFatalError(f"forbidden / quota exhausted ({code})")
        if code == 429:
            raise LLMRetryableError("rate limited (429)")
        if code >= 500:
            snippet = resp.text[:200] if resp.text else ""
            raise LLMRetryableError(f"server error {code}: {snippet}")
        if code >= 400:
            snippet = resp.text[:200] if resp.text else ""
            raise LLMFatalError(f"client error {code}: {snippet}")

        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
        except (KeyError, IndexError, json.JSONDecodeError, ValueError) as e:
            raise LLMError(f"malformed response: {e}; body={resp.text[:300]}") from e

        return ChatResult(
            content=content,
            model=data.get("model", payload["model"]),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            latency_ms=latency_ms,
            cache_hit=False,
        )
