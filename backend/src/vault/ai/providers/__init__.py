"""Language model providers behind one interface (plan section 9, ADR 0009).

``base`` holds the interface and its types; ``fake`` the offline, deterministic provider. The
Anthropic and OpenAI adapters come next; they are the only modules allowed to import an SDK.
"""
