# ADR 0120: Controlled serialized restart without historical execution migration

- Status: Proposed
- Date: 2026-10-30
- Decision owner/reviewers: repository owner; existing independent ReviewSensei reviewer
- Linked GitHub Issue: [#238](https://github.com/malsabbagh/review-sensei/issues/238)
- Linked PR: [#387](https://github.com/malsabbagh/review-sensei/pull/387)

## Context

A corrected package must be able to review the current full diff even when a
predecessor has an incompatible ledger, an unfinished published tail, or failed
publication. Restoring that predecessor is not evidence that its unknown costs,
native writes, findings or human obligations have been settled.

The selected implementation is a controlled procedure for identified stopped
writers, not a general crash-tolerant reset service. No durable coordinator or
new infrastructure is introduced.

## Decision

Keep the packaged restart registry empty by default. A separately reviewed row
binds repository/PR, base/head, trusted policy/workflow commits, one actual run
and job at attempt one, predecessor executions and exact opaque root hashes.
All ordinary review, reply and command entry in that registered scope is held.
The Worker refuses further capabilities to other executions. Available old
capabilities must be retired, or their documented expiry must pass after further
issuance is disabled. Existing installation-token privileges are not expanded.

`review-sensei-github restart-run` admits only that designation before ordinary
ledger discovery. It validates caller-supplied diff rejection before any startup
effects, confirms the identified runs/jobs have stopped, and archives bounded
opaque predecessor bytes with exclusive creation, fsync and exact readback.
The archive is abandoned execution history with unknown cost and unsettled
effects; it never grants executable authority. Fresh analysis uses a newly
prepared actual base/head Git diff, a fresh ledger and independent accounting.

Process-issued admission selects only the uniquely marked successor and pins
its object identity. Historical roots never become fallback authority. Every
mutation retains a current open-PR/base/head check. Its extra read is funded
inside the existing caller allowance without borrowing write, confirmation or
terminal tails. A crash consumes the one-shot slot; another process, absent
listing, changed head or rerun does not authorize another initializer.

Historical occupancy cannot be certified from opaque execution history. This
route therefore holds new inline findings and native approval. Fresh COMMENT
reviews without inline findings have a session-specific restart marker, included
inside the existing framing/byte allowance. Lost acknowledgement can reconcile
only an authenticated exact echo of this process's attempted payload. At most
one positive review identity is selected. Its original unknown observation and
charge remain; only that correlated effect is recognized as positively observed.

A clean new analysis cannot establish that historical obligations are satisfied.
Its completed native check therefore remains `action_required`; fresh required
fixes retain a failing conclusion. Ordinary unregistered review behavior is
unchanged. A successful transaction tail can close the new caller truthfully
without stamping the predecessor complete or approving the PR.

## Scope

This is a GitHub-backed ledger procedure for the existing hosted and local
inference runner kinds. It does not add an independent LocalSessionLedger reset,
automatic cutover, general retry-anywhere command, numerical comment-cap
activation, new workflow permissions or new capability types.

Within a registered scope, old migration/restoration startup is unreachable.
Generic compatible same-session recovery, read-only audit and existing public
migration APIs remain intact. Their removal would need replacement qualification
and explicit compatibility review; this ADR does not silently authorize it.

## Consequences

- Old delayed writes may still alter old GitHub objects. They cannot select or
  modify successor authority. Historical unknown liabilities are not free space.
- A vanished or competing successor refuses. A fresh finding review can retain
  analysis but cannot publish against unknown historical occupancy.
- A lost root/publication/finalization response does not justify replay or
  terminal success from absence. Exact positive current-process reconciliation
  is distinct from certifying an older execution.
- Unenumerated historical obligations need an explicit scoped disposition before
  a passing approval gate can be justified. Restart authorization is not one.
- The run directory must be retained storage under operator control. An
  ephemeral runner directory alone is not a durable archive/export strategy.
- Inactive reusable-workflow inputs select the dedicated restart entry for an
  explicit manual designation and omit the ordinary rescan intent. An existing
  self-hosted runner must supply an empty private retained directory outside
  its workspace/temp paths. Archive fsync and exact readback precede successor
  admission there; path validation does not certify actual storage retention.
  See [launch integration](../controlled-restart-launch.md).
- The proposed source-only launcher uses a protected prerequisite with no
  permissions, checkout, package installation, OIDC or provider credential of
  its own. The existing read-only frozen-package planning and identity preflight
  finish first and report the actual selected job, policy, base/head, run and
  workflow. Neither acquires a provider capability or discovers the old ledger.
  Every capability-bearing job then depends on the protected hold. This order
  makes registration use observed frozen values, not a future policy guess.
  Ordinary invocations proceed when the hold is skipped; cancellation or a
  failed/partial restart intent cannot admit a provider job.
- After the hold, a frozen-source installer verifies a later official hosted
  publication using the existing signed immutable tags, publisher execution,
  PyPI/Sigstore claims, exact wheel bytes and core pin. Ordinary installation
  and its same-source rule are unchanged. The separate restart binding comes
  only from the verified wheel's compiled row naming the actual first run/job,
  frozen workflow, policy and base/head. No unsigned registry loader exists.
- The exact row derives request_id and the retained child after the run exists.
  Both parent and child must already exist privately outside workspace/temp;
  the child is empty. The installer never creates or clears retained storage.
  Runtime admission repeats its own installed-row, current-scope, stopped-owner,
  expiry and one-shot checks. Installation does not issue a capability.
- Release verification uses a bounded read-only official-release metadata
  transport (48 requests, 512 KiB responses, 8 KiB paths, finite deadline).
  It cannot query PRs, issue comments or historical execution censuses. It is
  installation control-plane verification, not a replacement business transport:
  the original 512 lifecycle allowance and protected tails remain unchanged.
- `restart-caller` prints a manual caller for the existing workflow path without
  installing it or changing normal setup. `restart-handoff` is an optional offline
  worksheet from archive and publication bodies. It suggests same-instance or
  same-concern matches, preserves prior decisions as evidence, and never creates
  successor dispositions, clears obligations or grants approval. A missing
  match means not observed, not fixed. It is not a hidden backlog or public
  deferred-finding list. A qualified human must review the actual current head
  and historical objections under ordinary branch protections.
- Source implementation is not live activation. The protected environment,
  retained host, frozen release, later registered publication, matching Worker
  deployment, issuance cessation/expiry and actual canary still require exact
  independent review and separate live authority. Keep v5 fixed while held;
  publishing the later hosted package must not move it.

## Alternatives considered

Continuing arbitrary old-ledger migration couples new analysis to historical
execution repair. Deleting history or marking unknown work terminal invents
settlement. A durable coordinator could support general retries but exceeds the
selected code-only scope. A cooperative one-shot procedure preserves the narrow
guarantee and reports its interruption limits.

## Validation

Exercise real public CLI, caller meter, ledger, publisher, final check and terminal
tail with synthetic external services. Include actual local Git diff preparation,
both inference runner kinds, incompatible/published-unfinished/publication-failed
history, changed scope/policy, revoked publication capability, delayed old roots
and approvals/checks, uncertain occupancy, archive overflow, cross-process slot
contention, crashes and stored/unstored lost acknowledgements. Verify exact
archive bytes and physical observations, unchanged total512 and reserved tails,
no repeated POST/inference, and truthful authoritative terminal readback.

Repeat against rebuilt sdists/wheels outside the checkout with installed origin
and payload parity. Preserve failing receipts. Native Windows/Linux execution
requires those actual runtimes; passing Darwin tests or mocked platform labels
does not constitute their qualification. Worker admission remains separately
qualified through the existing synthetic OIDC endpoints.

## Rollout and rollback

Source/package publication is inert while the registry is empty. Independent
review and native platform checks precede a release through the existing sole
publisher. Live activation requires a separate exact designation, verified
archive retention and launch ordering, retirement of old writers/capabilities,
then one controlled canary. No activation, deployment, required-check setting,
credential change, native approval or merge is authorized by this ADR.

Rollback holds new admission. It preserves archives, successor state and unknown
effects; it never resurrects a predecessor or replenishes an abandoned slot.

## Follow-up work

Qualify native Windows/Linux environments, independently review the bounded
procedure, and review exact release/launch/archive retention before activation.
Keep strict publication selection in PR392 and finite capacity accounting in
PR393 separate. The denied1024-part/1536-comment/64-call census proposal remains
held. No old review or workflow is replayed as a smoke test.

The inactive publication-source V3 candidate in ADR0115 uses quarantine lineage
for eventual individual decisions and ordinary review completion. It does not
activate this retained-run launcher, relax its exact source/base binding, or
supply a completion path for the earlier deliberately held restart designation.
