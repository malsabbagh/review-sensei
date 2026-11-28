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

This matrix compares the generated v5 caller at the signed v5 cut
`40b4f52face3402fc3a9cd51387bde7cf53c3951` and the v6 coordinator callable at
the prepared signed cut `b8bf8668407263e9ceff67572c7ea271c90e5d73`.

| Entry point | v5 onboarding | Exact v6 and the proposed caller |
| --- | --- | --- |
| Automatic | Opened, reopened, synchronize, and ready-for-review. The reusable workflow skips drafts. | Same event types. Same-repository, non-draft only. The v6 runner skips fresh work when `github.writes=false` or automatic reviews are disabled. Default generated configuration leaves writes off. |
| Manual | Dispatch a selected PR with rescan or bounded review-remaining intent. | The callable accepts a dispatch event label. The proposed caller has no dispatch trigger, the new-install broker admits only PR events, and the callable has no review-remaining input. |
| Top-level mention | Authorized non-bot PR comments can request replies, rescans, review remaining, or supported commands. The v5 runner verifies the live source. | The callable accepts the `issue_comment` label but has no caller trigger, comment-source contract, or equivalent command/reply route in this channel. |
| Inline mention | A separate created review-comment trigger sends an authenticated inline reply context. | The callable rejects `pull_request_review_comment` in its preflight. The proposed caller has no inline trigger. |
| Fork | Automatic review is disabled. The v5 caller accepts and forwards a manual `head_repository` input. The metered hosted workflow preflight rejects a foreign or fork head. | Both automatic caller and v6 workflow exclude forks. The v6 validation step also rejects an explicit foreign head repository. |
| Uninstall | Manual cleanup opens a PR to remove the generated caller and configuration. Uninstalling the App is a separate GitHub action. | The existing cleanup still removes the shared caller path. It has no broker activation or coordinator drain operation. Reinstalls after a v6 activation require reconciliation of the old installation's slots and PR evidence before replacement. |

The v6 callee is `review-sensei-v6-pilot.yml`. The legacy
`review-sensei-run.yml` has the same bytes at both release commits. The v6
caller declares `checks: read` and `actions: read` in addition to the read
scopes and OIDC, which are required by the reusable workflow. Route retained
manual and comment behavior through the same v6 owner before enabling it. A
v5 event fallback on a v6-owned PR would break the single-writer contract.

## Rollout sequence

1. Decide the required parity for active default onboarding. Qualify the exact
   caller, Worker and callable through the project's protected release paths.
   If the missing behavior is in scope, qualify a new immutable release
   containing the callable changes. Verify the signed v6 object and peeled
   source, absence of a conflicting public ref, and public GitHub Actions
   reachability after authorized publication. Check that repository Actions
   policy allows the reusable workflow and the current manual cleanup. The
   prepared v6 tag can use the existing 0.9.0 / 0.3.35 release only for its
   qualified behavior.
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
