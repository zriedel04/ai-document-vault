"""The AI service (plan section 9): provider-neutral types, the providers and their registry, the
task-based router (``routing``), the egress gate, the usage ledger and, later, prompts and
citation validation.

Only ``vault.ai.providers`` may import a provider SDK (ADR 0009, ADR 0010); a test enforces it.
"""
