# ADR 0145 — Narrow Per-PR Coordinator Durable Object for v6 One-Writer Fencing

Status: Proposed
Date: 2026-11-27
Decision owner: ReviewSensei maintainers
Proposal: [docs/proposals/v6-one-writer-per-pr-audit-and-contract.md](../proposals/v6-one-writer-per-pr-audit-and-contract.md)
Frozen contract: [src/review_sensei/schemas/v6-coordinator-contract.json](../../src/review_sensei/schemas/v6-coordinator-contract.json)
Amends: [ADR 0020](0020-cloudflare-github-app-package.md), [ADR 0073](0073-authenticated-feedback-mutation-grants.md), [ADR 0144](0144-github-governed-recovery.md)

## Context

Under [ADR 0020](0020-cloudflare-github-app-package.md) (migration `v3`) and [ADR 0073](0073-authenticated-feedback-mutation-grants.md), the Cloudflare Worker (`deploy/cloudflare/`) removed `DeliveryLedger` and `BrokerLedger` and persisted zero state across invocations. [ADR 0144](0144-github-governed-recovery.md) subsequently defined ReviewSensei v6's PR-resident recovery ledger (`reviewsensei:v6:intent:v1`, `receipt:v1`, `unresolved:v1`, `progress:v1`) and dual-credential split, keeping the Cloudflare broker stateless.

However, a purely stateless Worker cannot prevent two concurrent or overlapping runners from simultaneously holding valid, unrevoked GitHub App installation tokens for the same pull request, nor can it remember an issued token's handle long enough to deterministically revoke it via `DELETE /installation/token` before admitting a successor writer. GitHub Actions workflow concurrency (`cancel-in-progress: false`) is best-effort routing and does not protect against delayed retries, manual dispatches, or network timeouts at the token mint boundary.

At the same time, ReviewSensei must NOT turn the Cloudflare Worker into a second review database or proxy PR writes:

- The pull request remains the **sole durable recovery ledger** for review findings, unresolved obligations, write intents, completion receipts, and hold markers.
- No PR diffs, source files, comment bodies, provider responses, provider keys, or plaintext installation tokens may ever be stored in Cloudflare Durable Object storage.
- `PUBLIC_WORKFLOW_TAG` remains pinned to `v5` (`40b4f52f`, `v0.8.28` / `github-v0.3.34`), and non-pilot repositories continue on the untouched `v5` path.

## Decision

Amend [ADR 0020](0020-cloudflare-github-app-package.md), [ADR 0073](0073-authenticated-feedback-mutation-grants.md), and [ADR 0144](0144-github-governed-recovery.md) to authorize **only** a narrow, per-PR SQLite Durable Object (`V6PrCoordinator`) in the Cloudflare Worker for **temporary per-PR credential custody, exclusive writer fencing, and deterministic token revocation**:

1. **Canonical per-PR coordination slot:**
   `V6PrCoordinator` is keyed strictly by `v6-pr:{host}:{app_id}:{installation_id}:{base_repo_id}:{pr_number}`. Because GitHub installation tokens are scoped to a repository plus permissions (not a pull request), this per-PR key is a ReviewSensei coordination invariant that requires trusted, pinned v6 workflow code, no independent write credentials on pilot repos, and zero PR-controlled code execution in the write job.
2. **Strict Keep vs. Never-Keep separation:**
   - **Coordinator SQLite storage keeps only:** slot state (`IDLE`, `RESERVED`, `MINTING`, `ACTIVE`, `DRAINING`, `RECONCILING`, `HOLD`), monotonic `generation` and `lease_epoch`, reservation and owner workflow/head metadata, `token_expires_at_ms`, `clock_margin_ms` (default `300000`), sanitized `last_reason_code`, and `encrypted_escrow` (installation token encrypted with `AES-GCM-256` using a key derived via `HKDF-SHA256` from `REVIEWSENSEI_SIGNING_KEY` and bound with AAD `{slot_key, generation, reservation_id}`).
   - **Coordinator SQLite storage never keeps:** plaintext installation tokens, GitHub App private keys, `REVIEWSENSEI_SIGNING_KEY`, raw AES/HKDF key material, OIDC JWTs, `GITHUB_TOKEN`, provider API keys, PR diffs, source contents, review comment bodies, or raw GitHub error bodies.
   - **PR ledger keeps only:** App-authored `reviewsensei:v6:{owner,intent,receipt,hold,unresolved,progress}:v1` HTML markers. It never keeps tokens, keys, or encrypted escrow payloads.
3. **Fenced issuance and deterministic revocation:**
   - Pre-mint `RESERVED -> MINTING` persistence prevents unescrowed mint races; uncertain mints enter `RECONCILING` (`v6_lease_mint_uncertain`).
   - Predecessor drain revokes the escrowed token via `DELETE /installation/token` (`204` confirmed -> `IDLE`; `401` -> expiry fallback only after `token_expires_at_ms + clock_margin_ms`; `429`/rate-limit `403` -> bounded reset backoff distinct from permission `403`; `5xx`/timeout -> bounded retry then `RECONCILING`/`HOLD` with `v6_lease_revocation_uncertain`).
   - Independent Worker proof sources use only OIDC claims, `GET /app`, `GET /repos/{owner}/{repo}/installation`, `GET /repos/{owner}/{repo}`, `GET /repos/{owner}/{repo}/pulls/{pr}`, `GET /repos/{owner}/{repo}/actions/runs/{run_id}`, `GET /repos/{owner}/{repo}/actions/runs/{run_id}/attempts/{attempt}`, and O(1) `GET /repos/{owner}/{repo}/issues/comments/{comment_id}` anchor verification — requiring zero new GitHub App permissions and zero new Wrangler secrets.
4. **Strict `v5` preservation and pilot isolation:**
   - `v5` remains pinned (`PUBLIC_WORKFLOW_TAG: "v5"`).
   - Opt-in pilot repositories listed in `V6_PILOT_REPOSITORIES` are rejected by v5 `TokenBroker.exchange` with `broker_v6_pilot_v5_writer_disabled` after a pre-admission 1-hour token drain so v5 and v6 writers can never overlap on the same PR.

## Scope

In scope for this ADR:

- Authorizing `V6PrCoordinator` SQLite Durable Object custody strictly for per-PR lease fencing and encrypted token escrow for revocation.
- Freezing the two-records split, state machine, HKDF/AES-GCM escrow format, `DELETE /installation/token` rules, 6 HTTP lease routes, and 15 sanitized diagnostic codes in [v6-coordinator-contract.json](../../src/review_sensei/schemas/v6-coordinator-contract.json).

Out of scope:

- Modifying or unpinning the `v5` tag (`40b4f52f`) or deploying live Worker changes in Phase 0–1.
- Adding GitHub App permissions, Wrangler secrets, or bulk PR reads/writes inside the Cloudflare Worker.
- Storing review findings, diffs, or recovery queues in Durable Object storage.

## Rollback and Validation

- Until all four Phase 0 blockers (`callable v6 workflow`, `fenced issuance`, `reproducible ambiguous-write recovery`, `opt-in GitHub qualification`) are closed and pilot validation passes, `V6_PILOT_REPOSITORIES` stays empty and `PUBLIC_WORKFLOW_TAG` stays `v5`.
- Removing a repository from `V6_PILOT_REPOSITORIES` and draining active leases returns it to the untouched `v5` channel without losing PR-resident recovery history.
