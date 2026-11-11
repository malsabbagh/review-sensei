# ADR 0137: Operational limits, title freshness, policy drift, and cross-PR quota boundaries

- Status: Proposed
- Date: 2026-11-11
- Decision owner/reviewers: ReviewSensei maintainers; independent source review
- Linked GitHub Issue: pending; lifecycle operational-boundary qualification (G09, G19, G20, G21, G25)
- Linked PR: pending; prospective contract qualification and recovery diagnostics

## Context

Five operational boundaries govern how ReviewSensei handles oversized review inputs,
pull request title mutations, pull request title byte budgets, default-branch policy
commit drift, and concurrent cross-PR / cross-repository execution under shared
provider or GitHub installation quotas:

1. **Oversized indivisible work (`G09`):** A single diff hunk, hunkless file change,
   or mandatory stage prompt can exceed configured prompt/diff byte budgets even
   after hunk subdivision. Silently truncating mandatory context or unilaterally
   raising budgets would violate coverage integrity and authorization limits.
2. **Pull request title freshness (`G19`):** Pull request titles can be edited before
   admission, during inference, before publication, after publication, or mid-way
   through a partial continuation (`@reviewsensei review remaining`). Treating every
   cosmetic title edit after publication as an inference cache miss wastes paid
   provider budget and destabilizes settled approvals, while ignoring a title change
   during an unfinished partial continuation would silently mix scopes.
3. **Pull request title UTF-8 byte limits (`G20`):** Routing-only workflow preflight
   does not validate pull request title bytes, shared hosted bootstrap enforces
   `256` UTF-8 bytes before session enrollment or provider spend, and direct local
   core/CLI entry enforces `4,096` UTF-8 bytes (`ReviewLimits.max_title_bytes`).
4. **Default-branch policy commit drift (`G21`):** Hosted bootstrap freezes the
   trusted default-branch policy commit `P` (distinct from reviewed `base_sha` `B`
   and `head_sha` `H`) and compares `P` against the live default-branch ref `P_live`
   before admission.
5. **Cross-PR and cross-repository quota ownership (`G25`):** GitHub Actions
   serializes runs per pull request (`reviewsensei-provider-review-<repo>-<pr>`,
   `cancel-in-progress: false`), while concurrent runs on distinct pull requests or
   repositories contend at the configured provider/account and GitHub App
   installation rate-limit boundaries without a global cross-repository lock service.

Each fail-closed boundary requires an explicit, machine-readable contract in
`review_sensei.operational_boundaries` paired with a deterministic, zero-corruption
recovery path.

## Decision

1. **Explicit indivisible-overflow refusal and bounded disposition (`G09`):**
   Keep explicit partial/refusal behavior for single oversized hunks, oversized
   hunkless files, and mandatory prompt overflows (`PromptContextOverflow`). Never
   truncate mandatory diff or prompt context and never expand configured token/byte
   budgets automatically. Classify every unprocessed unit in
   `operational_boundaries` with exact path, hunk coordinates, observed byte count,
   configured limit, and permitted recovery routes:
   - `@reviewsensei accept-coverage-uncertainty --reason "<justification>"` for
     authorized coverage-uncertainty disposition on the bound head/policy digest;
   - human file review confirmation (`human_file_review`); or
   - splitting the oversized change into smaller reviewable hunks/commits.
   Reserve `@reviewsensei review remaining` strictly for fitting units deferred by
   per-run call budgets or transient chunk failures, never for indivisible units that
   cannot fit a fresh budget.

2. **Dual-role pull request title freshness contract (`G19`):**
   - At fresh admission, bind the live authenticated pull request title into the
     invocation snapshot.
   - During in-flight inference and publication, complete truthfully against the
     bound snapshot title and never claim that an earlier review evaluated a title
     edited after admission.
   - After publication, treat pull request title edits as descriptive context in
     `build_review_context_cache_key`: preserve incremental baseline cache reuse
     (zero duplicate paid inference on title-only edits) and never invalidate a
     settled approval solely from a title edit, while verifying attribution against
     the title actually evaluated.
   - During `@reviewsensei review remaining` partial continuation, bind the normalized
     title in `_continuation_scope_digest`; refuse mixed-title continuation fail-closed
     and recover deterministically via explicit `@reviewsensei rescan` against the
     updated title.

3. **Intentional two-tier UTF-8 title byte boundary (`G20`):**
   Preserve and qualify the three entry surfaces using exact UTF-8 byte lengths
   (`len(title.encode("utf-8"))`, never character counts or silent truncation):
   - `routing_preflight`: unvalidated routing metadata; claims zero title validation
     credit;
   - `hosted_bootstrap` (`review`, `command`, and `reply`): `256` UTF-8 bytes
     enforced before session enrollment, comment mutation, or provider spend;
   - `direct_core` (local CLI and library `ReviewLimits.max_title_bytes`): `4,096`
     UTF-8 bytes.
   Shortening a pull request title to `<= 256` UTF-8 bytes restores hosted review,
   command, and reply execution immediately on the next trigger with intact session
   state.

4. **Strict default-branch policy commit identity before admission (`G21`):**
   Retain fail-closed commit-identity comparison between frozen policy commit `P` and
   live default-branch commit `P_live` before session enrollment, even when
   `.reviewsensei.yml` bytes are unchanged (`unrelated_default_branch_advance` vs
   `policy_content_changed` vs `malformed_policy_ref`). Keep `P` strictly distinct
   from reviewed `base_sha` `B` and `head_sha` `H` so ordinary PR base lag (`B != P`
   or `B != B_live` while `P == P_live`) remains admitted. Refusal mutates zero
   ledger state, spends zero provider budget, preserves prior settled/pending session
   records, never rebinds prior accounting to `P_live`, and recovers cleanly on the
   next run pinned to `P_live`.

5. **Per-PR serialization and account-owned cross-PR quotas (`G25`):**
   Preserve per-PR serialization (`reviewsensei-provider-review-<repo>-<pr>` with
   `cancel-in-progress: false` and `ReviewConcurrencyPlan.for_pull_request`) without
   adding a persistent global cross-repository lock service. Cross-PR and
   cross-repository concurrency and rate limits remain owned by the configured
   provider account, broker admission gate, and GitHub App installation quotas:
   - isolate capability tokens per run (`repr=False`, zero cross-PR credential
     sharing);
   - perform single physical broker dispatches (`retry_count=0`, zero duplicate
     dispatch storms) and surface typed HTTP `429` (`GitHubHTTPTransientError`) or
     HTTP `503` (`GitHubBrokerAdmissionHeldError`) refusals without disguising
     throttling as success;
   - release in-process `ProviderAdmission` slots deterministically and preserve
     retained `publication_pending` / `publication_failed` state when throttling
     occurs after inference so a post-cooldown retry replays publication with zero
     duplicate paid inference.

## Scope

Prospective contract qualification in `src/review_sensei/operational_boundaries.py`,
unit/integration regression suites for `G09`, `G19`, `G20`, `G21`, and `G25`, and
operator recovery documentation in `docs/operational-limits-and-quotas.md`. Existing
wire schemas, workflow definitions, broker endpoints, token scopes, and configured
resource budgets remain unchanged.

## Consequences

- Every fail-closed refusal across `G09`, `G19`, `G20`, `G21`, and `G25` names the
  violated invariant, preserves session/accounting integrity, and provides a tested
  bounded recovery action.
- Cosmetic pull request title edits after review publication do not burn duplicate
  inference budget or revoke settled approvals, while mid-continuation title edits
  still fail closed until `@reviewsensei rescan`.
- Hosted callers remain protected by the `256` UTF-8 byte pre-enrollment title guard
  across `review`, `command`, and `reply`, while direct local CLI/core callers retain
  the `4,096` UTF-8 byte limit.
- Default-branch advances during workflow startup require a fresh trigger pinned to
  the new default-branch commit rather than silently rebinding policy commit
  provenance.
- Source qualification in this ADR does not claim live runtime deployment or settle
  any historical incident.

## Alternatives considered

- **Truncating oversized hunks or auto-expanding prompt budgets (`G09`):** Rejected
  because silent truncation hides unreviewed code inside an apparent pass, and
  unapproved budget expansion violates cost and context controls.
- **Including pull request title in `build_review_context_cache_key` (`G19`):**
  Rejected because post-publication cosmetic title edits (such as prefixing a ticket
  key) would invalidate incremental cache entries and force duplicate paid provider
  inference on unchanged diffs.
- **Widening hosted bootstrap titles to `4,096` bytes or shrinking core to `256`
  bytes (`G20`):** Rejected; the `256`-byte hosted pre-work cap intentionally bounds
  untrusted event/API metadata before broker/session work while preserving `4,096`
  bytes for local CLI diffs and commit subjects.
- **Auto-refreshing `P` to `P_live` when `.reviewsensei.yml` bytes match (`G21`):**
  Rejected because repository context files and commit provenance bound to `P` can
  change outside `.reviewsensei.yml`.
- **Adding a centralized cross-repository lock service (`G25`):** Rejected because it
  introduces cross-repository state coupling and availability blast radius without
  changing the underlying provider/installation rate-limit owners.

## Validation

Deterministic unit and real-entry fixture suites validate both fail-closed refusal
and clean post-action recovery across all five boundaries:

- `tests/test_indivisible_overflow_g09.py`
- `tests/test_title_freshness_g19.py`
- `tests/test_title_byte_limits_g20.py`
- `tests/test_policy_commit_drift_g21.py`
- `tests/test_cross_pr_quota_g25.py`

## Rollout and rollback

Standard source review and deterministic CI checks apply. No workflow pin, broker
worker deployment, secret rotation, or schema migration is required. Rolling back
removes the qualification helpers, tests, and documentation without mutating stored
session comments or historical accounting records.

## Follow-up work

Keep live runtime readback (`G11`) and read-only historical incident classification
(`G03`, `G29`–`G31`) on their separate tracks; this prospective contract
qualification does not alter historical session ledgers.
