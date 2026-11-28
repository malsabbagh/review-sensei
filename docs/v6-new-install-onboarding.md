# v6 onboarding and v5 migration

The rollout cutoff applies to the current GitHub App installation. A MAC-verified
`installation.created` at or after the cutoff can propose v6 for a first install or a
deliberate reinstall. Continuously installed repositories keep their v5 caller.
Permission changes and repository additions use the existing v5 setup route for
repositories with no v6 proposal or activation. Leave `PUBLIC_WORKFLOW_TAG=v5`
while the v5 bridge is serving installations.

## Installation selection

The Worker matches the installation ID and creation time to the live GitHub
installation and the initial selection to stable repository IDs. A signed
continuation carries the original selection and release identity. A repository
that appears only in a later listing cannot enter the v6 cohort through that
continuation. A missing or oversized selection holds setup for review.

The proposal uses one GitHub setup PR per installation and repository. Its
deterministic branch is `review-sensei/setup-v6-<installation-id>`. A retry
reuses the existing proposal or respects its closure, including after cleanup.
A later install gets a distinct installation ID. An exact managed v5 workflow
can be replaced at its existing path. The replacement leaves repository
configuration, cleanup workflow, unrelated workflows, and PR recovery evidence
in place. An edited caller, competing setup PR, incomplete open-PR read, or
activation from a different installation generation holds the proposal.

The optional `V6_NEW_INSTALL_ONBOARDING` variable is a bounded JSON object.

| Field | Value |
| --- | --- |
| `app_id` | Numeric ID of the configured GitHub.com App |
| `setup_enabled` | Enables proposals. Defaults to false. |
| `start_at` | Rollout boundary in UTC as `YYYY-MM-DDTHH:MM:SSZ` |
| `tag_object`, `source_sha` | Exact signed annotated `v6` object and peeled immutable release commit |
| `activations` | Array, initially empty. Each entry has `installation_id`, `repository_id`, lowercase `repository`, the proposal's signed `receipt`, and optional observed UTC `fenced_at`. |

Unconfigured production keeps its current setup and broker behavior.
`setup_enabled=false` pauses post-cutoff proposals and preserves active
fences. The bounded activation list is for a limited rollout. Wider use needs
a reviewed way to maintain the same rollout evidence without expanding the PR
recovery ledger.

## Workflow compatibility

The public v5 cut is `40b4f52face3402fc3a9cd51387bde7cf53c3951`. The locally
prepared v6 cut at `b8bf8668407263e9ceff67572c7ea271c90e5d73` lacks the
entry points below. This candidate includes changes to both the hosted
`review-sensei-v6-pilot.yml` callable and its Worker.

| Entry point | v5 onboarding | Candidate v6 |
| --- | --- | --- |
| Automatic | Opened, reopened, synchronize and ready-for-review on an eligible PR. | Same events. A read-only plan verifies the live head, open non-draft PR and base/head repository identities. `automatic_reviews=false` skips the run. |
| Writes off | Review with a read-only GitHub token. | The coordinator checks admission for the exact head. The runner analyzes the immutable diff or verified conversation and writes its result to the Actions step summary. No reservation, writer credential, PR ledger entry, check, review or reply is produced. |
| Provider and runner | Hosted providers or local Ollama on the configured labeled runner. | Trusted default-branch configuration selects the same backend and labels. The plan and runner bind the same policy commit and provider settings. Local Ollama requires an available `self-hosted, linux, x64, ollama` runner and its configured model. Check availability for each cohort. |
| Manual | Dispatch a review on a selected PR, including full rescan or bounded review remaining. | Full review verifies the current maintainer, original event, default branch and live PR. It runs even if automatic reviews are disabled. A rerun reuses its operation ID. Remaining-only requests stop before inference because v6 lacks the retained coverage contract. |
| Top-level mention | Authorized PR comments can request a reply, full rescan, bounded remaining review or maintainer command. | The runner rereads the comment and verifies its author, association, PR, body digest and update time. A discussion gets one issue-comment reply. An explicit unedited full rescan additionally requires current maintainer permission. Remaining-only and recognized maintainer commands stop with a diagnostic. |
| Inline mention | A created review comment can request a thread reply. | After the same source validation, the runner verifies the root belongs to this PR and posts one native reply to that root. The receipt binds its exact body and parent. An uncertain reply is reconciled before any retry. Thread resolution remains manual. |
| Protected branch | A repository may require the head-bound native check named `ReviewSensei` from the ReviewSensei App, or an exact Actions job context. | The candidate publishes a PR review when writes are enabled. The native App check is outstanding. Hold migration for repositories whose branch rules require it or any v5 job context until the required name, numeric producer ID and head are qualified. Keep unknown protection requirements in the held cohort. |
| Fork and draft | Automatic fork reviews are disabled. The metered v5 preflight rejects foreign heads. | A fork or draft fails the callable's live preflight before coordinator access or analysis. The workflow never executes the PR head. |
| Uninstall | Manual cleanup opens a PR to remove managed files. | Cleanup still removes the same caller path. A previously activated generation requires a coordinator drain and reconciliation of its PRs before replacement. |

All supported writes pass through one coordinator slot for the PR. The owner,
result, intent and receipt stay on the PR. The callable forwards only the
credential required by the selected provider. GitHub queues pending work in
the per-PR concurrency group. Its documented limit is 100 pending runs.
[GitHub workflow identity](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#job-context),
[concurrency](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency),
[inline reply contract](https://docs.github.com/en/rest/pulls/comments#create-a-reply-for-a-review-comment).

Admit a cohort only after its required workflows and merge controls are
qualified. Remaining gaps include bounded review-remaining, disposition and
approval commands, evidence-backed human reassessment, automatic thread
resolution and native check publication. A discussion reply does not resolve a
finding. Keep v5 available for repositories using these features. Before
migration, an authorized administrator can compare effective branch protection
and rulesets against run metadata for a representative head. Record the exact
check context and GitHub App ID. A passing Actions job from a different
producer cannot satisfy a producer-pinned App check. Use count-only results for
progress reporting. [GitHub protected branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches#require-status-checks-before-merging).

## Rollout sequence

1. Qualify a fresh immutable release containing the caller, callable, installed
   runner and Worker changes together. Use protected exact-commit CI and
   CodeQL, release publication and both-package installed-consumer
   qualification. Check each eligible cohort's runner, provider, workflow
   permissions and required checks. Verify the new release tags and signatures.
   The prepared v6 tag points to the older release. Create a separately
   approved signed release-aligned v6 ref, reconcile any local/remote conflict
   explicitly, and verify its peeled commit and public callable reachability
   before enabling proposals. Preserve all immutable package tags.
2. Deploy the qualified Worker with proposals disabled. Verify the build,
   traffic, bindings, legacy v5 admission and the exact reachable v6 ref.
   Choose the cutoff when the serving rollout is ready. Enable proposals for
   authenticated post-cutoff lifecycle events.
3. Review the one setup PR and its signed receipt. Before activating it,
   resolve or explicitly hold each outstanding v5 publication, review session,
   and uncertain write on the repository. Check for customer-owned v5 callers
   and confirm that no old PR still needs v5 work. Leave a repository on v5
   when these checks are incomplete.
4. Add the receipt to `activations` with no `fenced_at`. Check that the v5
   writer and session grant fence is serving. Wait for requests begun on the
   previous deployment to finish before recording the observation time. The
   Worker requires a full hour after that time for older installation tokens
   to expire. Verify v6 status and the v5 write refusal, then merge the approved
   setup PR. The coordinator allows a new writer only on a PR created after
   this drain. Existing reviews and their evidence stay with their version.
5. Keep the activation while applying the existing acquisition hold and
   draining v6 for rollback or removal. Keep unresolved PRs in their current
   version. Restore the retained v5 caller only once leases are cleared. If
   v6 wrote PR effects, a strict per-PR rollback needs a v5 admission fence
   over those PRs or a reviewed forward recovery. A rollback before any v6
   effects can resume v5 after the drain.

## Existing repositories and v5 retirement

Start with repositories whose administrators explicitly choose migration. A
deliberate reinstall is the existing setup path when the whole installation
is in scope. For a shared installation, use a separately approved per-repository
setup PR and the same signed receipt, fence and drain. That entry point remains
to be implemented. Keep existing v5 references and broker support through
migration.

Count active App installations, repos verified on v5 or v6, unclassified
repos, v5 obligations awaiting resolution, and migrated repos with an active
rollback hold. Use the App owner's authorized installation inventory, default
branch workflow identities, existing setup PRs and retained PR evidence.
Publish only aggregate counts and coverage. Visible setup PRs and workflow
searches are partial observations. GitHub delivery retention is three days.
Personal security logs cover 90 days, and organization audit logs cover
180 days for organization owners. Record any gaps in the available audit
history and installation inventory. [Webhook delivery history](https://docs.github.com/en/webhooks/testing-and-troubleshooting-webhooks/viewing-webhook-deliveries), [personal security log](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/reviewing-your-security-log), [organization audit log](https://docs.github.com/en/organizations/keeping-your-organization-secure/managing-security-settings-for-your-organization/reviewing-the-audit-log-for-your-organization).

Request retirement only after a complete authorized active-inventory snapshot
shows zero v5 and zero unclassified consumers, zero unresolved or uncertain
v5 writes, drained writer credentials, passing exact-ref qualification for
required v6 workflow behavior, and a verified rollback or forward-recovery
path. Keep v5 serving whenever any outcome is unresolved. Retirement of the
broker bridge is a separate authorized production decision.
