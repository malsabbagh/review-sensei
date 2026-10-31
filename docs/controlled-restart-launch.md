# Controlled restart launch integration

This is an inactive operator procedure for the serialized restart in ADR 0120.
The installed registry remains empty. No dispatch, deployment, enrollment,
security-setting change or release is performed by this document.

## Source workflow routing

The reusable runner accepts two empty-by-default invocation inputs:
`controlled_restart_id` and `controlled_restart_directory`. They do not grant
authority. Both must be supplied together for a manual `workflow_dispatch`
review, without quarantine intent. Ordinary callers supply neither.

For an explicit restart, both inference runner paths select `restart-run`
instead of `review-run` and omit the ordinary rescan/remaining intent. The package
still verifies the installed designation, exact current identities, first attempt,
ceased predecessor executions and capability expiry before fresh admission.

The directory must already exist on an existing self-hosted runner. It must be
an empty, private directory owned by the runner user, named with the exact request
digest, outside both `RUNNER_TEMP` and `GITHUB_WORKSPACE`, with no symlinked path.
The workflow does not create, clear or reuse that retained slot. The operator
must independently establish that its underlying storage is retained; filesystem
path checks cannot establish a storage service's retention guarantee.

The restart command writes, fsyncs and reads back `abandoned-history.json` in that
directory before creating successor authority. A consumed/nonempty slot refuses.
Keep this directory and any independent backup through failure and rollback;
an optional seven-day diagnostics upload is not the retention contract.

Freshly prepared source diffs use a separate private ephemeral working directory,
removed on normal, error and cancellation exits. They are never placed in the
retained history slot, including when diagnostic artifacts are disabled. Abrupt
process termination may leave ephemeral working data for normal runner cleanup;
it does not clear or renew the retained archive/claim. Explicit diagnostic
artifacts retain their existing opt-in policy.

## Remaining launch and registration delivery gate

Routing and a retained archive location do not supply an actual designated run ID
or install a registration. The normal caller remains unchanged and cannot start a
restart. The implementation intentionally exposes no environment/CLI registry
loader, and the Worker registry is compiled into its deployment.

Before a live canary, review a concrete sequence that holds a real manual execution
before capability acquisition and before it requires the newly registered package.
Record its actual run ID, then supply the exact reviewed registration through
trusted release/installation and Worker deployment. The registration pins the
executing workflow SHA; do not create an impossible self-referential source binding
or predict the next Actions ID. Package, workflow and Worker provenance must agree.
No source overlay or unreviewed registry file becomes runtime authority.

A protected manual environment hold is one possible operator mechanism. It is not
configured here and may require a security-setting change. An approved existing
operator hold can also be assessed; this document grants neither mechanism.

After issuance is actually disabled for old executions, recheck bounded complete
writer cessation and revoke available capabilities or wait their documented expiry.
Recent bootstrap executions matter. Do not date the expiry window from the original
failed run. Overflow or incomplete observations refuse within the existing limits.

## Exact live confirmations still needed

The source-only contract A launcher is implemented but inactive. Prepare its
caller with `review-sensei-github restart-caller`; review the output for the existing
`.github/workflows/review-sensei-review.yml` path. Normal setup output is unchanged.
Installing this caller is a later explicit action, not part of printing it.

The manual inputs reserve exact `controlled_restart_core_version` and
`controlled_restart_hosted_version`, identify an approved
`controlled_restart_runner`, and name `controlled_restart_parent_directory`.
No request ID or archive child is predicted at dispatch. The reusable workflow's
hosted route uses the approved label, and its local route additionally retains
the existing self-hosted/Linux/x64/Ollama label requirements. The approved label
must select the qualified retained host; an arbitrary matching local runner is
not sufficient. Ordinary local routing retains its original four-label set.
The reusable workflow's
`restart_hold` environment is `reviewsensei-controlled-restart`; its protection
must be configured and independently checked before live use. Merely naming an
environment does not establish its protection or authorize a live run.

Release the empty-registry frozen launcher first. Start one approved manual
execution into its pre-capability hold. Record that actual run/job/attempt and
frozen workflow plus the read-only bootstrap's reported policy/base/head and
selected job, then independently review and publish the later hosted package
with that compiled row and matching Worker designation. Do not promote v5 during
this sequence. After exact registration and retirement/expiry approvals, release
the hold. The frozen installer verifies the later publication before installing
its exact wheels, derives the pre-created private child from the row and selects
`restart-run`. A failure does not authorize a rerun, new row or provider repeat.

The initial release and the later registered publication are separate artifacts.
The installer reuses existing official release verification; no new signer,
credential or permission is introduced. Public source/release reads have a finite
installation-only bound and cannot read PR history. All lifecycle business HTTP,
new root/publication readback and terminal accounting use the unchanged caller.

1. Release the reviewed source through the existing publisher at new immutable
   versions, with exact workflow/package/Worker provenance.
2. Identify the existing retained runner and archive path, and confirm its retention,
   permissions, ownership and backup responsibility. No new host is implied.
3. Approve the actual hold/launch mechanism and any necessary environment/security
   setting. No enabled writer is dispatched merely to discover its run ID.
4. Approve one exact registration and its trusted delivery, and the corresponding
   Worker deployment/issuance hold. Default-empty source publication is not this grant.
5. Approve stopping identified executions or revoking available tokens as applicable,
   retaining cessation/disable/expiry receipts.
6. Approve one designated canary naming current base/head, repository/PR, workflow,
   package versions, actual run/job/attempt, registry digest, archive destination and
   original finite allocation. The actual execution remains held until these agree.

The predecessor stays abandoned history with unknown cost and unsettled effects.
No terminal stamp, refund, historical deletion, native approval, branch-protection
change, merge, capacity expansion or automatic review loop follows from restart.

For the final human review, preserve the prior public finding dispositions and
read the actual successor review. An optional `restart-handoff --archive ...
--prior-body ... --prior-head ... --current-body ... --notes ...` worksheet proposes
matches using published instance/concern identities. Input bodies and notes are
offline evidence, not authenticated live inventory or commands. The worksheet
never transfers an override across heads or marks an absent concern fixed.
Old judgments need explicit revalidation. Ordinary bot review/reply/command entry
is still held by the installed designation: this worksheet cannot issue a formal
override or authorize another head/run. Any remaining code defect must be fixed;
a changed head requires a separately reviewed recovery/continuation action, not
an automatic repeat. Only a qualifying current-head reviewer
can supply the ordinary required approval. The controlled bot check can remain
`action_required` while historical obligations/occupancy remain unresolved.

TaskAlong's be1 executor/body-limit incident is separately source-qualified as a
prepublication overflow. It does not establish the published-tail exit proof for
PR395–397, and its executor/caller must not simply be appended to the c7 catalog.
No body-limit registration is activated by this launcher implementation.

## Review and qualification status

The source workflow's exact bytes are guarded by the trusted append-only
`REGISTRY_ONLY_WORKFLOWS` policy in `scripts/python_release_sources.py`. The changed
workflow must receive independent exact-artifact review before its digest is
recognized there. Do not derive a new trusted entry automatically from a candidate
or weaken the test that enforces recognition.

Local tests execute both actual Bash routes and feed their selected arguments
through the real restart lifecycle with synthetic external services. They verify
the retained archive, one inference and independent terminal accounting. This
qualifies source integration only. It is not a live retained-storage, hold, release,
deployment or registration-delivery receipt.
