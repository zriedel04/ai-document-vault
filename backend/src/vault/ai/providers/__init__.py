"""Language model providers behind one interface (plan section 9, ADR 0009).

``base`` holds the interface and its types; ``fake`` the offline, deterministic provider;
``anthropic`` the Anthropic adapter. The OpenAI adapter comes next. The adapters are the only
modules allowed to import an SDK.
"""
