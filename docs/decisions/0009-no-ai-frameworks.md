# ADR 0009: No LangChain or LlamaIndex

- Status: Accepted
- Date: 2026-10-01
- Hardware-dependent: no
- Plan reference: docs/architecture.md, sections 3 and 9, and section 22 (decision 11)

## Context

The retrieval pipeline, prompt construction and provider calls are the parts of this project the owner most wants to inspect, test and explain. Anthropic and OpenAI must both be supported, and models are routed by task through configuration.

## Decision

Use the official `anthropic` and `openai` Python SDKs behind the project's own small `LLMProvider` interface (`generate`, `stream`, `count_tokens`, with provider-neutral request and response types). Two adapters (Anthropic Messages API, OpenAI Responses API) translate the interface. A `FakeProvider` returns recorded responses so tests and CI need no keys, and a `LocalProvider` can be added later without other changes. No module other than the AI service imports a provider SDK. Prompts are versioned files, and the prompt version is stored with every result.

## Alternatives considered

- **LangChain or LlamaIndex**: abstraction churn and behaviour that is hard to debug, hiding exactly the parts that should be visible.
- **LiteLLM**: a useful proxy, but another dependency between the app and the provider APIs.

## Consequences

- A few hundred lines of adapter code to write and test.
- Provider-specific features (structured output, prompt caching, native citations) remain reachable.
- Retrieval and prompt logic is plain code covered by ordinary unit tests.
