# Operational Limits, Policy Drift, and Quota Recovery Guide

This guide documents the fail-closed operational boundaries formalized in
[ADR 0137](adr/0137-operational-limits-title-policy-and-quota-boundaries.md) and
implemented in `review_sensei.operational_boundaries`, together with the exact
deterministic recovery steps for each boundary.

## Summary of Boundaries

| Gap | Boundary | Enforced Limit / Invariant | Pre-Work / Ledger Guarantee | Deterministic Recovery Action |
|---|---|---|---|---|
| `G09` | Oversized indivisible work | Single hunk, hunkless file, or mandatory stage prompt exceeds configured diff/prompt byte budget | Zero silent truncation; zero automatic budget expansion; explicit partial coverage | Maintainer/admin (`maintain`/`admin` permission or `OWNER`/`MEMBER` association, `github.writes: true`) runs `@reviewsensei accept-coverage-uncertainty --reason "<why>"`, records human file review, or splits the oversized change |
| `G19` | Pull request title freshness | Title excluded from incremental cache key (`context.py`), bound verbatim in partial-continuation scope (`ReviewService._continuation_scope_digest`) | Post-publication title edits cost zero duplicate inference and keep settled approvals; mid-partial edits fail closed | Run `@reviewsensei rescan` if the title changed while `@reviewsensei review remaining` was pending |
| `G20` | Pull request title UTF-8 byte budget | `routing_preflight`: unvalidated; `hosted_bootstrap` (`review`/`command`/`reply`): `256` UTF-8 bytes; `direct_core`: `4,096` UTF-8 bytes | Hosted `> 256` UTF-8 bytes refuses before enrollment, ledger write, or provider call | Shorten the pull request title to `<= 256` UTF-8 bytes (or `<= 4,096` UTF-8 bytes in direct local CLI mode) and re-trigger |
| `G21` | Default-branch policy commit drift | Frozen default-branch policy commit `P` must equal live default-branch commit `P_live` before admission (independent of reviewed `base_sha` `B` and `head_sha` `H`) | Refuses before session enrollment or provider spend; prior session records remain untouched | Trigger a fresh event or `workflow_dispatch` targeting the default branch's current ref/commit `P_live` (never a stale SHA) |
| `G25` | Cross-PR & cross-repo quota ownership | Per-PR serialization (`reviewsensei-provider-review-<owner>/<repo>-<pr_number>`, `cancel-in-progress: false`); provider/account and installation own aggregate rate limits | Zero cross-PR credential sharing; `retry_count=0` (no duplicate dispatches); retained analysis preserved on HTTP `429`/`503` | Wait for rate-limit cooldown and re-run; post-retention publication throttling replays publication with zero duplicate inference |

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

### Verified source-level ceilings (`inspect_indivisible_boundary_contracts`)

| Boundary ID | Enforced Ceiling | Source Symbol |
|---|---|---|
| `per_request_diff_bytes` | `1,048,576` UTF-8 bytes | `review_sensei.validation.ReviewLimits.max_diff_bytes` |
| `legacy_unqualified_prompt_bytes` | `49,152` UTF-8 bytes | `review_sensei.budgets.LEGACY_ASSESSMENT_PROMPT_BYTES` |
| `unified_batch_prompt_bytes` | `262,144` UTF-8 bytes (`131,072` diff bytes) | `review_sensei.budgets.ReviewWorkBudgets.batch_prompt_bytes` |
| `qualified_token_window_input_ceiling` | `utf8_size(prompt) + framing_tokens + reserved_output_tokens + max(512, ceil(context_tokens / 20)) <= context_tokens` | `review_sensei.budgets.ProviderCapabilities.fits` |
| `per_run_provider_calls` | `8` calls default (`<= RESOURCE_BUDGET_MAX_PROVIDER_CALLS_CEILING`) | `review_sensei.outcomes.DEFAULT_RESOURCE_BUDGET_MAX_PROVIDER_CALLS` |

At exact `limit - 1` and `exact` UTF-8 bytes, mandatory pinned architecture rules and
diff hunks admit without truncation; at `limit + 1` UTF-8 bytes, admission refuses
with zero silent truncation and keeps incomplete coverage (`review_status == "partial"`,
`fully_reviewed == False`) strictly distinct from completion across core CLI and hosted
`review-run`.

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
   maintainer has inspected the oversized file/hunk out-of-band on the exact head SHA,
   policy digest, and enumerated `incomplete_coverage` token set. Authorization is
   enforced fail-closed by `authorized_maintainer` + `authorized_override`
   (`review_sensei.disposition`): the commenter must be a human non-bot collaborator
   with GitHub `maintain` or `admin` permission (or `OWNER` / `MEMBER` association
   when permission is unloaded), and repository policy must enable
   `github.writes: true` (ordinary `write` access alone, bots, the App itself, and
   casual commenters are rejected):
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
   - `ReviewService._continuation_scope_digest` (`src/review_sensei/service.py`) is the
     sole source of truth computing the canonical JSON SHA-256 (`evidence_digest`) over
     `ReviewRequest` (including `ReviewRequest.title`). `validate_bounded_text`
     validates UTF-8 byte bounds verbatim without whitespace collapsing, case folding,
     or Unicode NFC/NFKC normalization (hosted bootstrap omits an empty `""` title as
     `None` and binds any non-empty title byte-for-byte).
   - If the pull request title changes before `@reviewsensei review remaining`,
     continuation fails closed (`continuation scope does not match` in core
     `ReviewService`; `no reusable partial scope` at hosted `review-run` admission)
     rather than merging chunks reviewed under two different titles.

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
  (`DIRECT_CORE_MAX_TITLE_BYTES` / `ReviewLimits.max_title_bytes` and
  `ConversationContext.pull_request_title`, verified by `inspect_title_constant_contracts`).
- **Downstream reply prompt publisher (`hosted_conversation_publisher`):** After
  hosted bootstrap enforces the `256`-byte admission cap, if a pull request title is
  mutated mid-run above `MAX_CONTEXT_PR_TITLE_BYTES` (`2,048` UTF-8 bytes),
  `ConversationPublisher` appends an explicit `\n[truncated]` marker on a valid UTF-8
  codepoint boundary (never silent truncation).

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

- Refusal occurs before session enrollment or provider spend across hosted
  `review-run`, `command-run`, and `reply-run` (`default_policy_ref`), and
  `action_reply._configuration` additionally verifies that the checked-out
  `.reviewsensei.yml` SHA-256 matches `run.caller.accounting.configuration.digest`
  mid-run.
- Existing settled approvals, `FrozenApiConfiguration`, `startup.liability`,
  `completed` receipts, `blocks`, and `remaining` allowances on the pull request
  remain byte-for-byte intact across one or multiple consecutive default-branch races
  (`P0 -> P1 -> P2`, verified by `verify_policy_drift_funding_preservation`), and old
  run accounting is never rebound within a run (`ApiRunAccounting.require_binding`).
  When a subsequent run pinned to `P_live` rolls over a terminal predecessor, the
  predecessor's exact accounting is archived in `caller-run-root-v1` without refunding
  consumed or uncertain liability.
- In direct core execution, `RepositoryContextStore` and `load_configuration` bind
  context document SHA-256 digests into `ReviewService.context_cache_key` and
  `_continuation_scope_digest`, refusing stale `prior_partial` continuations when
  pinned context bytes change.

### Recovery runbook

- Trigger a fresh pull request event, `@reviewsensei rescan` comment, or manual
  `workflow_dispatch` targeting the default branch's current ref/commit `P_live` so
  checkout and admission bind to `P_live`.
- When using `workflow_dispatch`, explicitly select the default branch head ref (never
  re-run or dispatch against the failed run's stale commit SHA) and verify the workflow
  run's checked-out `ref` / `--policy-commit` input shows `P_live` before treating the
  run as recovered.

---

## 5. Cross-PR and Cross-Repository Quota Ownership (`G25`)

### Concurrency and rate-limit ownership model

- **Same pull request (`per_pr_workflow_serialization`):** GitHub Actions serializes
  `review`, `command`, and `reply` provider jobs per pull request in flat workflow
  group `reviewsensei-provider-review-<owner>/<repo>-<pr_number>` (`${{ github.repository }}`
  = `<owner>/<repo>`) with `cancel-in-progress: false` (matched by
  `plan_cross_pr_concurrency`), while core `ReviewConcurrencyPlan.for_pull_request`
  derives collision-free length-prefixed internal `workflow.key` and `provider.key`
  identifiers (`provider.cancel_in_progress=False`).
- **Across pull requests and repositories
  (`provider_and_installation_account_boundary`):** Distinct pull requests execute in
  independent concurrency groups without a global cross-repository lock service.
  Aggregate concurrency and request quotas are owned by the configured provider
  account, broker admission gate, and GitHub App installation boundaries.
- **Credential and dispatch containment:** Every run obtains isolated short-lived
  capability tokens (`repr=False` on `BrokerSession` and `BrokerSessionGrant`) and
  issues single physical broker requests (`retry_count=0`, zero duplicate dispatch
  storms).

### Read-only resource-owner inventory (`inspect_resource_owner_boundaries`)

| Boundary ID | Resource Owner Scope | Enforced Bound | Source Symbol |
|---|---|---|---|
| `per_pr_workflow_group` | Single pull request (`review` / `command` / `reply`) | `1` active workflow run (`cancel-in-progress: false`) | `.github/workflows/review-sensei-run.yml:concurrency` |
| `per_run_caller_http_allowance` | Single caller run + reserved successor on PR ledger | `DEFAULT_API_REQUEST_LIMIT = 512` (`startup + first_total + successor_total == limit`; `EvidenceBudgetExhausted` on exhaustion) | `review_sensei.api_work.DEFAULT_API_REQUEST_LIMIT` / `owner_allocations` |
| `per_operation_broker_control_budget` | Single hosted operation | `MAX_BROKER_BODY_BYTES = 262,144` bytes; `retry_count = 0` | `review_sensei_github.broker_client.MAX_BROKER_BODY_BYTES` |
| `github_installation_rate_limit` | GitHub App installation / `GITHUB_TOKEN` | External GitHub API rate limit; fail-closed on HTTP `429`/`503` | `review_sensei_github.http.GitHubHttp` |
| `provider_account_or_local_endpoint` | Hosted provider account or manual CLI (`review_sensei.cli.main`) provider / local Ollama host | `DEFAULT_RESOURCE_BUDGET_MAX_PROVIDER_CALLS = 8`; non-zero exit without partial-as-complete output on HTTP `429`/`503` | `review_sensei.outcomes.ResourceBudget` / `review_sensei.providers` |

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
