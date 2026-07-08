"""
LLM subsystem.

Layered design:
  L1 services/   business use cases (assessment, encourage, ...)
  L2 client_*    LLMClient interface + provider implementations
  L3 prompts/    versioned prompt files (.md)
  L4 横切         cache / sanitizer / audit / budget / retry

Entry point: `from backend.llm import get_client`
"""
