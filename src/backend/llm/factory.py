"""
LLM client factory — reads config/llm.toml + secrets.env, returns configured client.

Business code calls:
    client = factory.get_client("assessment")
    cfg    = factory.get_config("assessment")     # for temperature/max_tokens

Mock fallback is automatic when:
  - provider="mock" in config
  - DEEPSEEK_API_KEY not set / placeholder value / empty
  - any unexpected provider name

So forgetting to set the key doesn't crash; just degrades to mock.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from threading import Lock

try:
    import tomllib                    # Python 3.11+
except ImportError:
    import tomli as tomllib           # fallback for older

from .client_base import LLMClient
from .client_deepseek import DeepSeekClient
from .client_mock import MockClient

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_LLM_CONFIG   = _PROJECT_ROOT / "config" / "llm.toml"
_SECRETS      = _PROJECT_ROOT / "config" / "secrets.env"

# Placeholder values we treat as "not set"
_PLACEHOLDERS = {"", "sk-replace-me", "your-api-key-here", "REPLACE_ME"}


# ---- Config loading (cached, mtime-aware) ---------------------------

_cfg_cache: tuple[float, dict] | None = None
_cfg_lock = Lock()


def _load_config() -> dict:
    global _cfg_cache
    with _cfg_lock:
        if not _LLM_CONFIG.exists():
            return {}
        mtime = _LLM_CONFIG.stat().st_mtime
        if _cfg_cache and _cfg_cache[0] == mtime:
            return _cfg_cache[1]
        with _LLM_CONFIG.open("rb") as f:
            data = tomllib.load(f)
        _cfg_cache = (mtime, data)
        return data


def _load_secrets_into_env() -> None:
    """Read config/secrets.env (lines KEY=value) and inject into os.environ.

    Already-set env vars (e.g. from systemd EnvironmentFile=) take precedence,
    so secrets.env is just a convenient fallback for dev.
    """
    if not _SECRETS.exists():
        return
    try:
        for line in _SECRETS.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception as e:
        log.warning("Failed to load secrets.env: %r", e)


_load_secrets_into_env()


# ---- Public API -----------------------------------------------------

def get_config(use_case: str = "default") -> dict:
    """Return the effective config dict for a use case (default-inherited)."""
    cfg = _load_config()
    default_cfg = dict(cfg.get("default", {}))
    use_cfg = dict(cfg.get(use_case, {}))
    if use_cfg.pop("inherit", None) == "default":
        merged = default_cfg
        merged.update(use_cfg)
        return merged
    return use_cfg or default_cfg


def get_client(use_case: str = "default") -> LLMClient:
    """Return an LLMClient configured for this use case."""
    eff = get_config(use_case)
    provider = eff.get("provider", "mock").lower()

    if provider == "mock":
        return MockClient()

    if provider == "deepseek":
        api_key_env = eff.get("api_key_env", "DEEPSEEK_API_KEY")
        api_key = os.environ.get(api_key_env, "").strip()
        if api_key in _PLACEHOLDERS:
            log.warning("[LLM] %s not set (or placeholder); using mock client.",
                        api_key_env)
            return MockClient()
        try:
            return DeepSeekClient(
                api_key=api_key,
                base_url=eff.get("base_url", "https://api.deepseek.com/v1"),
                default_model=eff.get("model", "deepseek-chat"),
                timeout_s=float(eff.get("timeout_s", 30)),
            )
        except Exception as e:
            log.error("[LLM] DeepSeek client init failed: %r; falling back to mock", e)
            return MockClient()

    log.warning("[LLM] Unknown provider %r; using mock", provider)
    return MockClient()


def current_provider(use_case: str = "default") -> str:
    """Return the actual provider name that get_client(use_case) will use.

    Useful for health checks / UI badges.
    """
    eff = get_config(use_case)
    provider = eff.get("provider", "mock").lower()
    if provider == "deepseek":
        api_key_env = eff.get("api_key_env", "DEEPSEEK_API_KEY")
        api_key = os.environ.get(api_key_env, "").strip()
        if api_key in _PLACEHOLDERS:
            return "mock (deepseek key missing)"
    return provider
