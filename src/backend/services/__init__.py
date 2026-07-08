"""
Business use cases that invoke LLM.

Each service is responsible for:
  - Loading the right prompt template
  - Building messages with sanitized inputs
  - Calling LLMClient.chat()
  - Returning a domain-specific result type
"""
