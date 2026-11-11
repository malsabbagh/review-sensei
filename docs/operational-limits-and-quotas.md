# Operational Limits, Policy Drift, and Quota Recovery Guide

This guide documents the fail-closed operational boundaries formalized in
[ADR 0137](adr/0137-operational-limits-title-policy-and-quota-boundaries.md) and
implemented in `review_sensei.operational_boundaries`, together with the exact
deterministic recovery steps for each boundary.

## Summary of Boundaries

| Gap | Boundary | Enforced Limit / Invariant | Pre-Work / Ledger Guarantee | Deterministic Recovery Action |
|---|---|---|---|---|
| `G09` | Oversized indivisible work | Single hunk, hunkless file, or mandatory stage prompt exceeds configured diff/prompt byte budget | Zero silent truncation; zero automatic budget expansion; explicit partial coverage | Run `@reviewsensei accept-coverage-uncertainty --reason "<why>"`, record human file review, or split the oversized change |
| `G19` | Pull request title freshness | Title excluded from incremental cache key (`context.py`), bound in partial-continuation scope (`service.py`) | Post-publication title edits cost zero duplicate inference and keep settled approvals; mid-partial edits fail closed | Run `@reviewsensei rescan` if the title changed while `@reviewsensei review remaining` was pending |
| `G20` | Pull request title UTF-8 byte budget | `routing_preflight`: unvalidated; `hosted_bootstrap` (`review`/`command`/`reply`): `256` UTF-8 bytes; `direct_core`: `4,096` UTF-8 bytes | Hosted `> 256` UTF-8 bytes refuses before enrollment, ledger write, or provider call | Shorten the pull request title to `<= 256` UTF-8 bytes (or `<= 4,096` UTF-8 bytes in direct local CLI mode) and re-trigger |
| `G21` | Default-branch policy commit drift | Frozen default-branch policy commit `P` must equal live default-branch commit `P_live` before admission (independent of reviewed `base_sha` `B` and `head_sha` `H`) | Refuses before session enrollment or provider spend; prior session records remain untouched | Re-dispatch or trigger the next pull request / maintainer command event against the current default-branch commit `P_live` |
| `G25` | Cross-PR & cross-repo quota ownership | Per-PR serialization (`reviewsensei-provider-review-<repo>-<pr>`, `cancel-in-progress: false`); provider/account and installation own aggregate rate limits | Zero cross-PR credential sharing; `retry_count=0` (no duplicate dispatches); retained analysis preserved on HTTP `429`/`503` | Wait for rate-limit cooldown and re-run; post-retention publication throttling replays publication with zero duplicate inference |

---

## 1. Oversized Indivisible Work (`G09`)

### When it triggers

- **Oversized single hunk (`oversized_hunk`):** A single diff hunk (even after any
  eligible hunk subdivision) exceeds `max_diff_bytes_per_call` or available call
  context bytes.
- **Oversized hunkless file (`oversized_hunkless_file`):** A file-level change
  without subdividable diff hunks exceeds the per-call budget.
- **Mandatory prompt overflow (`mandatory_prompt_overflow`):** Fixed stage
  instructions, categories, or required metadata raise `PromptContextOverflow` before
  variable diff hunks can fit.
- **Non-patch or budget boundaries (`binary_or_non_text`, `generated_excluded`,
  `incomplete_file_enumeration`, `call_budget_exhausted`):** Classified distinctly so
  operators know whether remaining units can fit a continuation run.

### Why `@reviewsensei review remaining` is gated

`@reviewsensei review remaining` is only offered when remaining units fit within a
single call budget and were deferred by per-run call ceilings or transient chunk
errors. Offering `review remaining` for an indivisible unit that exceeds the per-call
byte ceiling would loop without progress.

### Recovery runbook

Choose one of the following bounded actions:

1. **Split the change:** Break generated snapshots, lockfiles, or monolithic
   refactors into smaller reviewable commits or hunks below the configured byte
   budget, then push a new head commit.
2. **Record authorized coverage-uncertainty disposition:** When an authorized
   maintainer has inspected the oversized file/hunk out-of-band on the exact head SHA
   and policy digest:
   ```text
   @reviewsensei accept-coverage-uncertainty --reason "Reviewed oversized migration hunk manually"
   ```
3. **Human file review:** Complete required human review on the flagged paths in the
   native pull request review flow.

---

## 2. Pull Request Title Freshness (`G19`)

### Lifecycle timing semantics

1. **Edited before admission (`before_admission`):** Bootstrap reads the live
   authenticated pull request payload and binds the updated title into the review
   snapshot.
2. **Edited during inference (`during_inference`):** The active run finishes
   evaluating against the title bound at admission and records truthful attribution to
   that snapshot title.
3. **Edited before publication (`before_publication`):** Publication attributes the
   summary to the snapshot title actually evaluated rather than claiming the newly
   edited live title was analyzed.
4. **Edited after publication (`after_publication`):**
   - `build_review_context_cache_key` excludes cosmetic pull request title text so
     incremental follow-up runs and cache lookups reuse prior baseline evidence with
     **zero duplicate paid inference**.
   - Settled approvals are not invalidated solely by a pull request title edit.
   - Published attribution helpers verify whether an earlier review evaluated an
     earlier title (`verify_published_title_attribution`).
5. **Edited during partial continuation (`during_partial_continuation`):**
   - `_continuation_scope_digest` in `ReviewService` binds the normalized pull request
     title into the partial checkpoint scope.
   - If the pull request title changes before `@reviewsensei review remaining`,
     continuation fails closed (`continuation scope does not match`) rather than
     merging chunks reviewed under two different titles.

### Recovery runbook

- If `@reviewsensei review remaining` refuses after a pull request title edit,
  either restore the original pull request title to finish the existing partial
  checkpoint or trigger a clean full evaluation under the new title:
  ```text
  @reviewsensei rescan
  ```

---

## 3. Pull Request Title UTF-8 Byte Limits (`G20`)

### Entry-surface contract

Byte measurement is strictly `len(title.encode("utf-8"))` with zero silent
truncation:

- **Workflow routing preflight (`routing_preflight`):** Unvalidated routing check;
  claims zero title validation credit.
- **Hosted bootstrap (`hosted_bootstrap` — `review`, `command`, and `reply`):**
  Enforces **`256` UTF-8 bytes** (`HOSTED_BOOTSTRAP_MAX_TITLE_BYTES`) before session
  enrollment, issue-comment ledger writes, or provider calls. Multi-byte UTF-8
  codepoints (2-byte accented characters, 3-byte CJK characters, 4-byte emoji) are
  counted by encoded UTF-8 byte width.
- **Direct local CLI / core (`direct_core`):** Enforces **`4,096` UTF-8 bytes**
  (`DIRECT_CORE_MAX_TITLE_BYTES` / `ReviewLimits.max_title_bytes`).

### Recovery runbook

1. Edit the pull request title so its UTF-8 byte length is `<= 256` bytes for hosted
   GitHub Actions runs (move long details into the pull request description body).
2. Re-run the failed workflow or post the maintainer command (`@reviewsensei rescan`,
   reply action, or synchronize event). Because the prior refusal happened before
   session enrollment, no ledger cleanup is needed.

---

## 4. Default-Branch Policy Commit Drift (`G21`)

### Commit-identity invariant

Hosted bootstrap separates three Git object identities:

- `P` (`frozen_policy_commit`): trusted default-branch commit checked out for
  `.reviewsensei.yml` and repository policy context;
- `P_live` (`live_policy_commit`): current `refs/heads/<default_branch>` commit on
  GitHub at bootstrap verification;
- `B` and `H` (`reviewed_base_sha` and `reviewed_head_sha`): pull request diff base
  and head commits.

A pull request whose `B` lags behind `main` (`B != P` or `B != B_live`) is **normal
and admitted** whenever `P == P_live`. Admission fails closed only when `P != P_live`
(whether `.reviewsensei.yml` is byte-identical or modified) or when the default-branch
ref object is not a valid 40-hex commit SHA.

### Pre-work safety guarantees

- Refusal occurs in `_configuration()` before session enrollment or provider spend.
- Existing settled approvals and pending session transactions on the pull request
  remain byte-for-byte intact, and old run accounting is never rebound to `P_live`.

### Recovery runbook

- Trigger a fresh workflow dispatch, pull request event, or `@reviewsensei rescan`
  comment so checkout and admission bind to the current default-branch commit
  `P_live`. Do not retry a stale workflow job pinned to an outdated default-branch
  checkout.

---

## 5. Cross-PR and Cross-Repository Quota Ownership (`G25`)

### Concurrency and rate-limit ownership model

- **Same pull request (`per_pr_workflow_serialization`):** GitHub Actions serializes
  `review`, `command`, and `reply` provider jobs per pull request in group
  `reviewsensei-provider-review-<owner>/<repo>-<pr_number>` with
  `cancel-in-progress: false`, matching `ReviewConcurrencyPlan.for_pull_request`.
- **Across pull requests and repositories
  (`provider_and_installation_account_boundary`):** Distinct pull requests execute in
  independent concurrency groups without a global cross-repository lock service.
  Aggregate concurrency and request quotas are owned by the configured provider
  account, broker admission gate, and GitHub App installation boundaries.
- **Credential and dispatch containment:** Every run obtains isolated short-lived
  capability tokens (`repr=False` on `BrokerSession` and `BrokerSessionGrant`) and
  issues single physical broker requests (`retry_count=0`, zero duplicate dispatch
  storms).

### Stage-specific recovery runbook after HTTP `429` / `503`

1. **Throttled at broker bootstrap before work (`broker_admission_before_work`):**
   - **Effect:** `GitHubBrokerAdmissionHeldError` (HTTP `503` temporary hold) or
     `GitHubHTTPTransientError` (HTTP `429` / `503`) exits non-zero before session
     enrollment or provider calls (`provider_calls == 0`).
   - **Recovery:** Wait for the broker/installation rate-limit window to reset and
     re-trigger a fresh workflow run (`@reviewsensei rescan` or next event).
2. **Throttled during provider inference (`provider_inference`):**
   - **Effect:** Run exits non-zero without disguising starvation/throttling as a
     clean review; in-process `ProviderAdmission` leases release deterministically.
   - **Recovery:** Wait for provider quota cooldown, then run
     `@reviewsensei review remaining` (if a partial checkpoint was retained) or
     `@reviewsensei rescan`.
3. **Throttled after inference retention during publication
   (`post_retention_publication`):**
   - **Effect:** Inference output is already durably retained in the session
     transaction (`publication_pending` / `publication_failed`), and the run exits
     non-zero rather than dropping findings.
   - **Recovery:** Re-run the failed workflow attempt after cooldown; ReviewSensei
     replays publication from the retained result to `publication_succeeded` with
     **zero duplicate paid inference**.
