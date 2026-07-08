"""
LLMClient interface — the only abstraction the business code sees.

All real-world concerns (provider, network, retry, cache, audit, budget) live
behind this interface. Business code uses `LLMClient.chat(messages)` and
nothing else.

Error hierarchy lets callers distinguish retryable vs fatal failures:
  LLMRetryableError — network / 5xx / 429 / timeout  → safe to retry
  LLMFatalError     — 4xx / invalid key / quota     → don't retry
  LLMError          — base class for everything else
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ChatMessage:
    role: str                # "system" | "user" | "assistant"
    content: str


@dataclass
class ChatResult:
    content: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    cache_hit: bool = False


class LLMError(Exception):
    """Base class for all LLM-related failures."""


class LLMRetryableError(LLMError):
    """Network / timeout / 5xx / rate-limit — caller may retry."""


class LLMFatalError(LLMError):
    """Bad key / 4xx / quota exhausted — retrying won't help."""


class LLMClient(ABC):
    """Single async method — all providers implement this."""

    @abstractmethod
    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout_s: float | None = None,
    ) -> ChatResult:
        ...
