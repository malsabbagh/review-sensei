# ADR 0144 — GitHub-governed v6 recovery architecture

Status: Proposed
Date: 2026-11-26
Decision owner: ReviewSensei maintainers
Proposal: [docs/proposals/v6-github-governed-recovery.md](../proposals/v6-github-governed-recovery.md)

## Context

Under the `v5` (`caller-requests-v2`) architecture, `owner_allocations()` partitions a fixed `DEFAULT_API_REQUEST_LIMIT = 512` ceiling (`startup=130`, `successor=191`, `terminal=50`, leaving `first_work=141`) alongside static `MAX_PAGINATION_PAGES` recovery caps. Multi-round pull requests routinely hit that artificial read cliff even while unused caller budget and thousands of GitHub API requests remain available. Raising static request caps merely relocates the cliff, while relying on single-comment `PATCH` updates as pseudo-CAS risks losing pending write intents or completion receipts if two writers ever overlap.

## Decision

ReviewSensei v6 replaces fixed recovery read and page-count ceilings with GitHub-governed, progress-guarded recovery built on seven architectural invariants:

1. **Dual credentials with an explicit 5-item broker App-read allowlist.** Bulk PR and repository scans execute exclusively on the workflow's read-only `GITHUB_TOKEN` (`contents: read`, `pull-requests: read`, `issues: read`, `checks: read`, `actions: read` — never `contents: write` and never falling back to the App token). Mutations and a strictly enumerated 5-item `O(1)` App-read allowlist (`GET /app`, live PR binding/head/base/state fence, collaborator permission lookup, active-writer run-liveness fence, and targeted effect verification on ambiguous/lost writes) stay behind the stateless Cloudflare App broker (`deploy/cloudflare/wrangler.jsonc` migration `v3`).
2. **Exclusive writer ownership and immutable intent/receipt records.** GitHub comment `PATCH` provides no compare-and-swap precondition. Every write boundary proves exclusive writer ownership against live PR state and workflow run identity (`repository_id`, `pull_request`, `operation_id`, `run_id`, `run_attempt`, `writer_epoch`, `head_sha`). Non-idempotent external writes record immutable App-authored write intents beforehand and immutable completion receipts upon confirmation or reconciliation; compact read-phase progress hints never hold the sole copy of an unresolved intent, receipt, or review result.
3. **Stability-verified pagination with rescan-and-deduplicate fallback.** Pagination scans in deterministic ascending order and resumes from a saved position only when prefix and position stability are provable (e.g., GraphQL `totalCount` plus verified prefix digest/anchor chain). Whenever stability cannot be proven (REST offset shifts, earlier comment deletions, changed counts, or head drift), recovery rescans the affected range and deduplicates deterministically by `(stable_record_id, content_sha256)`.
4. **Persistent cross-restart no-progress loop guard.** Measurable progress requires verifying a new distinct `(stable_record_id, content_sha256)` record, reconciling an immutable intent to a receipt, or advancing to a later recovery phase. `stall_count`, `last_progress_digest`, and `transient_error_count` persist on the PR so restarts cannot reset the loop guard.
5. **PR-resident unresolved results with zero fallback model calls.** The pull request is the sole durable ledger for unresolved findings, pending obligations, overflow companion prose reviews, and immutable intents/receipts. Actions Artifacts are strictly optional cache copies; if an artifact is missing or expired, recovery hydrates from the PR, and if required PR evidence itself is missing, recovery holds and identifies the exact missing evidence without ever starting a fresh model call as a fallback.
6. **REST and GraphQL HTTP 200 throttling separated from 403 permission errors.** Quota tracking separates `GITHUB_TOKEN` and the App installation token. REST `403`/`429` rate-limit headers and GraphQL HTTP `200` `RATE_LIMITED` responses are classified as throttling, whereas `403` responses without rate-limit indicators fail closed immediately as permission errors without retry or App fallback.
7. **Maintainer-rerun resume in v6.0 and `v5` pinned until release gate.** Short rate-limit waits honor `Retry-After`/reset within the job cap; long waits persist durable PR state, publish a `paused-rate-limited` check status with `resume_after`, and exit cleanly for a maintainer rerun (`@reviewsensei verify` or Actions rerun), keeping the broker 100% stateless. `PUBLIC_WORKFLOW_TAG` remains pinned to `v5` until all v6 release gates pass.

## Consequences

- Long-lived and multi-round PRs recover without artificial 75-call or 512-call read ceilings and without burning App installation quota on bulk pagination.
- Permission misconfigurations (`403`) fail closed with exact `(endpoint_id, credential, missing_scope)` diagnostics instead of masking scope gaps behind App fallback.
- Stale or overlapping runners cannot overwrite active intents or receipts, and lost write responses reconcile idempotently without duplicate reviews or fallback model calls.
- Because `checks: read` and `actions: read` are new read-only `GITHUB_TOKEN` scopes, the v6 reusable workflow and `tests/test_ci_policy.py` must admit them explicitly while preserving zero `contents: write` on `GITHUB_TOKEN`.

## Validation and rollout

Rollout keeps `v5` pinned on `caller-requests-v2` and proceeds in gated phases:

1. **Phase 0 (Credential & Permission Matrix):** Codify and exhaustively test `V6_ENDPOINT_CREDENTIAL_MATRIX`, `BROKER_APP_READ_ALLOWLIST`, and `authorize_v6_endpoint_credential` across all endpoints, workflow events (`pull_request`, `issue_comment`, `workflow_dispatch`), and repository visibilities (`public`, `private`, `fork_pull_request`).
2. **Phase 1 (Dual-Credential Transport & Classifiers):** Track independent quotas and verify REST `403`/`429` plus GraphQL HTTP `200` `RATE_LIMITED` throttling separate from `403` permission failures.
3. **Phase 2–3 (Writer Fencing, Stability Pagination & Reconciliation):** Enforce exclusive writer fencing, immutable intents/receipts, stability-verified pagination with rescan+dedup, cross-restart loop guards, write-response-as-readback, and reconcile-before-retry.
4. **Phase 4 (Release Gate):** Require green end-to-end tests covering >512-call recovery, mid-recovery comment deletion, loop-guard halting, throttling on both credentials, stale-writer rejection, lost-write reconciliation, missing-evidence holds, and artifact-free restarts before unpinning `v5`.
