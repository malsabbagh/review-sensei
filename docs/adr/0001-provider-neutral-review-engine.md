# ADR 0001: Provider-neutral review engine

- Status: accepted
- Date: 2026-08-02

## Context

Code Sensei needs to review pull-request diffs with Ollama while keeping the
business logic reusable if the project later supports other model providers or
non-GitHub publishers.

Embedding provider calls and GitHub delivery in one workflow would make prompt
rules, output validation, and security behavior difficult to test and migrate.

## Decision

Keep the review domain and `ReviewService` independent of network and GitHub
libraries. Define a small `ReviewProvider` protocol that translates a generic
`ProviderRequest` into a normalized `ProviderResponse`. Register concrete
providers explicitly through `ProviderRegistry`.

Ollama is the first adapter. Its endpoint, bearer authentication, JSON mode,
non-streaming behavior, and timeout handling remain inside `OllamaProvider`.

The service validates the model result before returning `ReviewResult`, including
requiring inline locations to refer to added or modified diff lines. A future
GitHub publisher consumes only that validated result.

## Consequences

Positive:

- Provider and publisher integrations can change independently.
- Core tests do not require network credentials.
- Invalid model output fails closed before external writes.
- Local Ollama and Ollama Cloud share one adapter.

Tradeoffs:

- A future provider needs an explicit adapter and registry entry.
- A hosted GitHub App and publisher still need to be implemented separately.
- Provider-specific capabilities cannot leak into the core contract without a
  deliberate interface change.
