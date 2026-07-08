"""
Privacy sanitization for data leaving K1 to external LLMs.

The K1 stores patient data locally only. When we send context to DeepSeek
for assessment / encourage generation, we strip identifying fields and
replace patient name with a stable hash.

Hash properties:
  - Deterministic: same name → same hash (so LLM responses are stable)
  - Short: 6 hex chars after a "p" prefix (~16M collisions per device, fine)
  - One-way: practically irreversible without rainbow-tabling Chinese names
"""

from __future__ import annotations

import hashlib


def hash_patient_id(name: str) -> str:
    """Stable short hash of patient name. p7f3a2 style."""
    if not name:
        return "p_anon"
    h = hashlib.sha256(name.encode("utf-8")).hexdigest()
    return "p" + h[:6]


def sanitize_session_data(session: dict, patient_name: str) -> dict:
    """Return a copy of session safe to send to LLM."""
    out = dict(session)
    out["patient"] = hash_patient_id(patient_name)
    # Strip any explicit PII fields if leaked into the dict
    for k in ("patient_name", "name", "real_name", "phone", "email", "id_card"):
        out.pop(k, None)
    return out
