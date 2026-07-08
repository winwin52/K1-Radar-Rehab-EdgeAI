"""
Prompt loader with hot reload + Jinja2 templating.

Prompt files live in `prompts/<name>.md`. Format:

    # system
    You are a helpful assistant...

    # user
    Patient: {{ patient_id }}
    Duration: {{ duration_min }} minutes
    ...

Loader behavior:
  - On each call, stat() the file; reload if mtime changed
  - Split by `# system` / `# user` / `# assistant` headers
  - Render each section through Jinja2 with the given variables
  - Return list[ChatMessage] ready for LLMClient.chat()

If Jinja2 isn't installed, falls back to simple `{{var}}` string substitution
(no expressions, no conditionals, no loops).
"""

from __future__ import annotations

import re
from pathlib import Path
from threading import RLock
from typing import Any

try:
    from jinja2 import Template, StrictUndefined
    HAS_JINJA = True
except ImportError:
    HAS_JINJA = False

from .client_base import ChatMessage


_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_PROMPT_DIR = _PROJECT_ROOT / "prompts"
_SECTION_RE = re.compile(r"^#\s*(system|user|assistant)\s*$",
                         re.MULTILINE | re.IGNORECASE)


class _PromptCache:
    """Caches parsed sections per file; reloads on file mtime change."""

    def __init__(self):
        self._cache: dict[str, tuple[float, list[tuple[str, str]]]] = {}
        self._lock = RLock()

    def load(self, filename: str) -> list[tuple[str, str]]:
        path = _PROMPT_DIR / filename
        with self._lock:
            try:
                mtime = path.stat().st_mtime
            except FileNotFoundError as e:
                raise FileNotFoundError(f"Prompt file not found: {path}") from e

            entry = self._cache.get(filename)
            if entry and entry[0] == mtime:
                return entry[1]

            raw = path.read_text(encoding="utf-8")
            parsed = _parse_sections(raw)
            self._cache[filename] = (mtime, parsed)
            return parsed


def _parse_sections(text: str) -> list[tuple[str, str]]:
    """Split markdown by # system / # user / # assistant headers."""
    matches = list(_SECTION_RE.finditer(text))
    if not matches:
        # No section markers → treat the whole file as a user message
        return [("user", text.strip())]
    parts: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        role = m.group(1).lower()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content = text[start:end].strip()
        parts.append((role, content))
    return parts


_cache = _PromptCache()


def render_messages(prompt_file: str, variables: dict[str, Any]) -> list[ChatMessage]:
    """Load prompt file + render its sections with the given variables."""
    sections = _cache.load(prompt_file)
    out: list[ChatMessage] = []
    for role, template in sections:
        rendered = _render_string(template, variables)
        out.append(ChatMessage(role=role, content=rendered))
    return out


def _render_string(template: str, variables: dict[str, Any]) -> str:
    if HAS_JINJA:
        # StrictUndefined → noisy errors if a {{var}} isn't supplied
        return Template(template, undefined=StrictUndefined).render(**variables)
    # Tiny fallback: literal {{var}} substitution only
    out = template
    for k, v in variables.items():
        out = out.replace(f"{{{{{k}}}}}", str(v))
        out = out.replace(f"{{{{ {k} }}}}", str(v))
    return out
