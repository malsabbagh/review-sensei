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
   `V6PrCoordinator` is keyed strictly by `v6-pr:{host}:{app_id}:{installation_id}:{base_repo_id}:{pr_number}`, where `host` is canonicalized to a lowercase ASCII hostname with no port or path (`^v6-pr:[a-z0-9.-]+:[1-9][0-9]*:[1-9][0-9]*:[1-9][0-9]*:[1-9][0-9]*$`). Because GitHub installation tokens are scoped to a repository plus permissions (not a pull request), this per-PR key is a **ReviewSensei coordination boundary** that requires trusted, pinned v6 workflow code, no independent write credentials on pilot repos, and zero PR-controlled code execution in the write job.
2. **Strict Keep vs. Never-Keep separation:**
   - **Coordinator SQLite storage keeps only:** slot state (`IDLE`, `RESERVED`, `MINTING`, `ACTIVE`, `DRAINING`, `RECONCILING`, `HOLD`), monotonic `generation` and `lease_epoch`, reservation and owner workflow/head metadata, `token_expires_at_ms`, `clock_margin_ms` (default `300000`), sanitized `last_reason_code`, and `encrypted_escrow` (installation token encrypted with `AES-GCM-256` using a key derived via `HKDF-SHA256` from `REVIEWSENSEI_SIGNING_KEY` and bound with AAD `{slot_key, generation, reservation_id}`).
   - **HKDF-SHA256 domain separation & rotation:** Token escrow (`salt="v6-pr-coordinator-escrow-v1"`, `info="reviewsensei:v6:token-escrow:v1"`) and owner-anchor attestation (`salt="v6-pr-coordinator-owner-attestation-v1"`, `info="reviewsensei:v6:owner-attestation:v1"`) use distinct HKDF domain-separation constants. Rotating `REVIEWSENSEI_SIGNING_KEY` requires quiescing acquisitions, draining/expiring all active slots to `IDLE` or `HOLD` first; any unrotated ciphertext fails closed to `HOLD` with `v6_lease_escrow_lost` until expiry + `clock_margin_ms`.
   - **Coordinator SQLite storage never keeps:** plaintext installation tokens, GitHub App private keys, `REVIEWSENSEI_SIGNING_KEY`, raw AES/HKDF key material, OIDC JWTs, `GITHUB_TOKEN`, provider API keys, PR diffs, source contents, review comment bodies, or raw GitHub error bodies.
   - **PR ledger keeps only:** App-authored `reviewsensei:v6:{owner,intent,receipt,hold,unresolved,progress}:v1` HTML markers (`<= 65536` UTF-8 bytes, with `<` and `>` escaped as `\u003c` and `\u003e` so literal `-->` / `<!--` never appear in payload). It never keeps tokens, keys, or encrypted escrow payloads.
3. **Fenced issuance, guarded transitions, and deterministic revocation:**
   - Pre-mint `RESERVED -> MINTING` persistence prevents unescrowed mint races; uncertain mints enter `RECONCILING` (`v6_lease_mint_uncertain`).
   - `/github/v6/lease/release` is valid **only** in `ACTIVE` for the matching verified owner (`slot_key`, `generation`, `lease_epoch`, `reservation_id`); non-`ACTIVE` states fail closed with HTTP `409`.
   - `RECONCILING -> IDLE` is valid **only** via `/github/v6/lease/complete-reconcile` with a terminal `reconciliation_outcome` (`"adopted"` or `"proved_not_submitted"`) and confirmed credential clearance; non-terminal outcomes (`"ambiguous_hold"`, `"incomplete"`) transition `RECONCILING -> HOLD` without clearing obligations or advancing generation.
   - `HOLD -> IDLE` / `HOLD -> RECONCILING` are valid **only** via `/github/v6/lease/resolve-hold` with confirmed credential clearance and verified named proof (`"reconciled_clean"` or `"enter_reconcile"`), preventing unconditional bypasses or `RECONCILING <-> HOLD` oscillation.
   - Predecessor drain revokes the escrowed token via `DELETE /installation/token` (`204` confirmed -> `IDLE`; `401` with `null`/invalid `token_expires_at_ms` -> fail closed to `HOLD` with `v6_lease_escrow_lost`; `401` with valid `token_expires_at_ms > 0` -> `IDLE` only if `now_ms >= token_expires_at_ms + clock_margin_ms`, else `RECONCILING`; `429`/rate-limit `403` -> bounded reset backoff distinct from permission `403`; permission `403` and `http_other_4xx` -> fail closed to `HOLD` with `v6_lease_revocation_uncertain` without clearing escrow; `5xx`/timeout -> bounded retry then `RECONCILING`).
   - Independent Worker proof sources use only OIDC claims, `GET /app`, `GET /repos/{owner}/{repo}/installation`, `GET /repos/{owner}/{repo}`, `GET /repos/{owner}/{repo}/pulls/{pr}`, `GET /repos/{owner}/{repo}/actions/runs/{run_id}`, `GET /repos/{owner}/{repo}/actions/runs/{run_id}/attempts/{attempt}`, and O(1) `GET /repos/{owner}/{repo}/issues/comments/{comment_id}` anchor verification — requiring zero new GitHub App permissions and zero new Wrangler secrets.
4. **Strict `v5` preservation, pilot isolation, and rollback invariant:**
   - `v5` remains pinned (`PUBLIC_WORKFLOW_TAG: "v5"`).
   - Opt-in pilot repositories listed in `V6_PILOT_REPOSITORIES` are rejected by v5 `TokenBroker.exchange` with `broker_v6_pilot_v5_writer_disabled` after a pre-admission 1-hour token drain so v5 and v6 writers can never overlap on the same PR.
   - Rollback (`v6 -> v5`) must add the repo to `V6_PILOT_ROLLBACK_REPOSITORIES` first to block new v6 acquisitions, drain/reconcile every slot for that repo until all slots are `IDLE` with zero unresolved `HOLD` obligations, and only then remove the repo from `V6_PILOT_REPOSITORIES` (and `V6_PILOT_ROLLBACK_REPOSITORIES`).

## Scope

In scope for this ADR:

- Authorizing `V6PrCoordinator` SQLite Durable Object custody strictly for per-PR lease fencing and encrypted token escrow for revocation.
- Freezing the two-records split, state machine and transition guards, HKDF/AES-GCM escrow and owner-attestation domain separation, `DELETE /installation/token` rules, 6 HTTP lease routes, and 15 sanitized diagnostic codes in [v6-coordinator-contract.json](../../src/review_sensei/schemas/v6-coordinator-contract.json).

Out of scope:

- Modifying or unpinning the `v5` tag (`40b4f52f`) or deploying live Worker changes in Phase 0–1.
- Adding GitHub App permissions, Wrangler secrets, or bulk PR reads/writes inside the Cloudflare Worker.
- Storing review findings, diffs, or recovery queues in Durable Object storage.

## Rollback and Validation

- Until all four Phase 0 blockers (`callable v6 workflow`, `fenced issuance`, `reproducible ambiguous-write recovery`, `opt-in GitHub qualification`) are closed and pilot validation passes, `V6_PILOT_REPOSITORIES` stays empty and `PUBLIC_WORKFLOW_TAG` stays `v5`.
- Rolling back a pilot repository follows the ordered `V6_PILOT_ROLLBACK_REPOSITORIES` drain invariant above so no v5 and v6 tokens can overlap and PR-resident recovery history stays intact.
