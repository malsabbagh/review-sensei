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
   serializes runs per pull request
   (`reviewsensei-provider-review-<owner>/<repo>-<pr_number>`,
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
   budgets automatically (`inspect_indivisible_boundary_contracts` verifies
   `ReviewLimits.max_diff_bytes = 1,048,576`, `LEGACY_ASSESSMENT_PROMPT_BYTES = 49,152`,
   `ReviewWorkBudgets.batch_prompt_bytes = 262,144`, `ProviderCapabilities.fits`
   exact token-window reserve math up to `4,194,304` `context_tokens`, and
   `DEFAULT_RESOURCE_BUDGET_MAX_PROVIDER_CALLS = 8`, keeping `review_status == "partial"`
   and `fully_reviewed == False` distinct from completion across core CLI and hosted
   `review-run`). Classify every unprocessed unit in
   `operational_boundaries` with exact path, hunk coordinates, observed byte count,
   configured limit, and permitted recovery routes:
   - `@reviewsensei accept-coverage-uncertainty --reason "<justification>"` for
     authorized coverage-uncertainty disposition on the bound head, policy digest,
     and exact `incomplete_coverage` token set (authorized strictly via
     `authorized_maintainer` + `authorized_override`: a human non-bot actor with
     GitHub `maintain` or `admin` collaborator permission—or `OWNER`/`MEMBER`
     association when permission is unloaded—plus `github.writes: true`; ordinary
     `write` access, bots, the App itself, and casual commenters are rejected);
   - human file review confirmation (`human_file_review`); or
   - splitting the oversized change into smaller reviewable hunks/commits.
   Reserve `@reviewsensei review remaining` strictly for fitting units deferred by
   per-run call budgets or transient chunk failures, never for indivisible units that
   cannot fit a fresh budget.

2. **Dual-role pull request title freshness contract (`G19`):**
   - At fresh admission, bind the live authenticated pull request title into the
     invocation snapshot across core (`ReviewRequest`, `ConversationContext`,
     `review_sensei.cli.main`) and hosted (`review-run`, `command-run`, `reply-run`)
     entry points.
   - During in-flight inference and publication, complete truthfully against the
     bound snapshot title and never claim that an earlier review evaluated a title
     edited after admission.
   - After publication, treat pull request title edits as descriptive context in
     `build_review_context_cache_key`: preserve incremental baseline cache reuse
     (zero duplicate paid inference on title-only edits) and never invalidate a
     settled approval solely from a title edit, while verifying attribution against
     the title actually evaluated.
   - During `@reviewsensei review remaining` partial continuation, bind the exact
     `ReviewRequest.title` field in `ReviewService._continuation_scope_digest` (the
     sole owner computing canonical JSON SHA-256 via `evidence_digest` over
     `ReviewRequest`, where `validate_bounded_text` preserves UTF-8 codepoints
     verbatim without whitespace collapsing, case folding, or Unicode NFC/NFKC
     normalization, and hosted bootstrap omits empty `""` as `None`); refuse
     mixed-title continuation fail-closed (`continuation scope does not match` in
     core `ReviewService`; `no reusable partial scope` at hosted `review-run`) and
     recover deterministically via explicit `@reviewsensei rescan` against the
     updated title.

3. **Intentional two-tier UTF-8 title byte boundary (`G20`):**
   Preserve and qualify the entry surfaces using exact UTF-8 byte lengths
   (`len(title.encode("utf-8"))`, never character counts or silent truncation,
   verified at runtime by `inspect_title_constant_contracts`):
   - `routing_preflight`: unvalidated routing metadata; claims zero title validation
     credit;
   - `hosted_bootstrap` (`review`, `command`, and `reply`): `256` UTF-8 bytes
     (`HOSTED_BOOTSTRAP_MAX_TITLE_BYTES`) enforced before session enrollment, comment
     mutation, or provider spend;
   - `direct_core` (local CLI and library `ReviewLimits.max_title_bytes` and
     `ConversationContext.pull_request_title`): `4,096` UTF-8 bytes
     (`DIRECT_CORE_MAX_TITLE_BYTES` / `CONVERSATION_CONTEXT_MAX_TITLE_BYTES`);
   - downstream `ConversationPublisher` reply prompt cap (`2,048` UTF-8 bytes,
     `CONVERSATION_PUBLISHER_TITLE_CAP_BYTES`): appends an explicit `\n[truncated]`
     marker on a valid UTF-8 codepoint boundary only if a title mutates mid-run after
     the `256`-byte hosted admission gate.
   Shortening a pull request title to `<= 256` UTF-8 bytes restores hosted review,
   command, and reply execution immediately on the next trigger with intact session
   state.

4. **Strict default-branch policy commit identity before admission (`G21`):**
   Retain fail-closed commit-identity comparison between frozen policy commit `P` and
   live default-branch commit `P_live` before session enrollment across `review-run`,
   `command-run`, and `reply-run` (plus `action_reply._configuration` mid-run policy
   digest verification and core `RepositoryContextStore` / `load_configuration`
   context-digest binding), even when `.reviewsensei.yml` bytes are unchanged
   (`unrelated_default_branch_advance` vs `policy_content_changed` vs
   `malformed_policy_ref`). Keep `P` strictly distinct from reviewed `base_sha` `B`
   and `head_sha` `H` so ordinary PR base lag (`B != P` or `B != B_live` while
   `P == P_live`) remains admitted. Refusal mutates zero ledger state, spends zero
   provider budget, preserves prior settled/pending session records and original
   `FrozenApiConfiguration` / `startup` / `blocks` / `remaining` funding across single
   and consecutive default-branch races (`P0 -> P1 -> P2`, verified by
   `verify_policy_drift_funding_preservation`), never rebinds prior accounting to
   `P_live` (`ApiRunAccounting.require_binding`), and recovers cleanly on the next run
   pinned to `P_live` (for manual `workflow_dispatch`, operators must select the
   default branch's current ref/commit `P_live` rather than re-dispatching a stale
   SHA).

5. **Per-PR serialization and account-owned cross-PR quotas (`G25`):**
   Preserve per-PR serialization (`reviewsensei-provider-review-<owner>/<repo>-<pr_number>`
   in GitHub Actions with `cancel-in-progress: false`, paired with collision-free
   length-prefixed internal keys in `ReviewConcurrencyPlan.for_pull_request`) without
   adding a persistent global cross-repository lock service. Cross-PR,
   cross-repository, and manual CLI concurrency and rate limits remain owned by their
   source-verified boundaries (`inspect_resource_owner_boundaries`):
   - conserve per-run caller HTTP request tranches (`DEFAULT_API_REQUEST_LIMIT = 512`
     split via `owner_allocations` across `startup + first_total + successor_total`,
     with `EvidenceReadBudget` failing closed via `EvidenceBudgetExhausted`);
   - isolate capability tokens per run (`repr=False`, `MAX_BROKER_BODY_BYTES = 262,144`,
     zero cross-PR credential or ledger sharing);
   - perform single physical broker dispatches (`retry_count=0`, zero duplicate
     dispatch storms) and surface typed HTTP `429` (`GitHubHTTPTransientError`) or
     HTTP `503` (`GitHubBrokerAdmissionHeldError`) refusals—as well as non-zero fail-closed
     exits on manual `review_sensei.cli.main` / `OllamaProvider` HTTP `429`/`503`—without
     disguising throttling as success;
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

CLI fixtures use explicit temporary configuration and a credential-free runtime
environment, so checkout or inherited provider settings cannot bypass the intended
boundary. Synthetic diff files retain exact LF UTF-8 bytes on every platform.
The G25 broker fixture sends four distinct numeric repository/PR identities through
one shared synthetic account allowance, records every physical request (including
holds), and retries only held identities after the fake quota window resets. It
does not assert an ordering/fairness guarantee. Separate command/review/reply
fixtures use the same target identity and prove route isolation only, not cross-PR
ledger isolation. The real manual CLI adapter's HTTP 429/503 controls compare its
run-outcome provider/retry counters with observed dispatches and bounded Retry-After
sleeps. These fixtures do not prove live all-writer containment or deployment.

## Rollout and rollback

Standard source review and deterministic CI checks apply. No workflow pin, broker
worker deployment, secret rotation, or schema migration is required. Rolling back
removes the qualification helpers, tests, and documentation without mutating stored
session comments or historical accounting records.

## Follow-up work

Keep live runtime readback (`G11`) and read-only historical incident classification
(`G03`, `G29`–`G31`) on their separate tracks; this prospective contract
qualification does not alter historical session ledgers.
