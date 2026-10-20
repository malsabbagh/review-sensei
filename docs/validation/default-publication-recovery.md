# Default publication rollout and recovery

This repairs the actual default writer and caller. It does not activate another
publication host or qualify every possible 250-finding review.

## Reader-first rollout

1. Keep `github.publication_inventory: legacy`. Build and verify engine and GitHub
   adapter from the same reviewed source. Run publisher, command/reassessment,
   retained-recovery, installed wheel and extracted-sdist compatibility checks.
2. After separately authorized release, install updated readers in every caller:
   reusable hosted and local jobs, commands, delayed verification, human feedback,
   installed CLI and recovery jobs. Confirm the exact installed runtime. Old readers
   reject large indexes; they are not a safe fallback for an existing large review.
3. Only after those consumers are verified, obtain maintainer authorization to set
   trusted `github.publication_inventory: complete`. Both reusable lanes forward
   this value. This implementation leaves repository configuration on `legacy`.
4. Roll back new emission to `legacy` if necessary, retaining the updated readers.
   Do not downgrade readers while large reviews remain in active histories.

The complete profile preserves every finding and full explanation within the
existing 65,536-byte review, individual-item, future-assessment and path bounds.
Compact 250 entries can fit; arbitrary rich 250 entries need not. Overflow remains
blocked and retains the validated result when retention is enabled.

## Recover without inference

With `github.operation_entry: enabled`, the default application retains only the
final validated transaction-bound ReviewResult in strict-owned PR result parts.
`github.artifacts: none` still disables diagnostics containing reviewed diff and
trusted contexts. Those contexts remain required from the trusted caller.

Run the installed GitHub adapter against the same repository, numeric repository
ID, PR, base/head, trusted configuration/evidence documents and independently
selected session ledger. Use a read-only metadata token through `GITHUB_TOKEN` or
`GH_TOKEN`; mutations continue through the existing broker. For example:

```sh
review-sensei-github github review --recover-retained \
  --repository OWNER/REPO --repository-id REPOSITORY_ID --pull-request PR \
  --head-sha EXACT_HEAD --base-sha EXACT_BASE --base-branch main \
  --diff trusted.patch --configuration-context trusted-configuration.json \
  --evidence-context trusted-evidence.json --github-session-ledger \
  --publication-inventory complete --allow-write --enable-review
```

Use `--session-ledger PATH` instead for an existing serialized local ledger.
Verification-round admission still requires the original trusted
`--admission-context`; retained bytes do not recreate or authorize it. Keep the
ordinary accepted-operation guard enabled. No provider is constructed by this
recovery path. One fresh publication-attempt budget spans all its phases; it does
not reset at each read or publication step.

### Explicit manual reusable-caller recovery

The explicitly prepared recovery caller in
`examples/github-actions/review-sensei-review.yml` has an optional
`recover_publication` input, defaulting to false. Default Python/Worker setup
emission and the installed repository caller remain at exact released
pre-recovery bytes until the compatible callable channel is verified. Select it
only for an original committed result whose
publication is pending or failed. Supply `recovery_policy_commit` (the full
original trusted default-branch policy commit) and `recovery_expected_head`
(the full original PR head). The caller still uses `operation: review`; it
does not request a rescan or start another analysis round. Ordinary review
retries continue to report `publication_recovery_required` without emitting a
new result or context document.

Both hosted and local reusable lanes require manual `workflow_dispatch`,
the unchanged original live head, an exact base, and an original policy commit
that is an ancestor of the currently frozen trusted default branch. Current
trusted write/publication policy still applies. The lanes regenerate the diff
with `prepare-diff` from the exact base/head, skip provider credential and
inference steps, and call the installed package's existing publication path:

```sh
review-sensei-github publish --recover-retained --reconstruct-contexts \
  --trusted-context-root original-trusted-policy \
  --repository OWNER/REPO --repository-id REPOSITORY_ID --pull-request PR \
  --head-sha EXACT_HEAD --base-sha EXACT_BASE --base-branch main \
  --diff trusted.patch --github-session-ledger --allow-write --enable-review
```

`--reconstruct-contexts` recomputes the original canonical caller's secret-free
configuration, evidence policy, stage/lens/learning context key and deterministic
reservation using the independently frozen source. It checks them against the
authenticated committed transaction before reading result parts. The original
prior baseline must still be authenticated and available. A completed verification
checkpoint may have replaced it: that case refuses with an instruction to supply
the original admission context through the existing explicit context-document
publication route. Reconstruction also refuses changed source/defaults, original
overrides it cannot reproduce, unknown reservations and missing context. It never
derives expected configuration from the retained result, current provider labels,
or a different head's baseline. The existing CLI `--diff` input remains trusted
caller evidence; the reusable workflow always regenerates it from checked commits.

Context/ledger reads, retained result loading, accepted-operation validation,
retention reconciliation and publication all use one publication-attempt budget.
Recovery keeps broker OIDC, the broker-selected comment ledger, accepted-result
checks, ownership, exact-head checks, replay fencing and limits enabled. A large
history can still exhaust its budget and refuse. No learning PR is generated.

Release and validate in this order:

1. Qualify and release the additive reusable runner and matching runtime.
2. Independently verify the callable `v5` channel's exact immutable source and
   declared input types. The released runner at `fb235279` has 19 inputs and
   rejects all three new recovery keys, including false/empty values. A package
   release or same-source template parity does not establish channel compatibility.
3. After that verification, make a normal reviewed upgrade of the installed
   caller and default Python/Worker emitters. The prepared Python/Worker recovery
   template and example are the later activation target, not current defaults.

Moving `v5` alone cannot add dispatch inputs to an installed caller. Exact managed
callers can receive the later upgrade through ordinary setup reconciliation;
operator edits and future setup versions remain owned and are not overwritten.
An older deployed installer keeps its older caller until its separately qualified
rollout. No broker capability, permission or secret changes are required.

### Bounded publication and optional cleanup continuation

Publication success and approval eligibility are separate. A completed COMMENT
review may correctly retain required fixes, human decisions or partial-baseline
facts. Optional removal of superseded old threads runs only after durable
publication success, with fresh ownership, head, native root, full prose and
latest-eligibility fences. Current human threads are never cleanup targets.

An exact captured-history qualification at PR316's original base/head measured
three canonical retained-recovery invocations under the unchanged 60 ordinary /
64 total request limit: the first retained one complete native review but refused
before the success-ledger write; the second reused that review and committed
success without duplicates or inference; the third retained success but had no
headroom for optional old-thread cleanup. This is bounded recovery over multiple
invocations, not a claim of first-invocation completion or arbitrary-history
capacity. A failure with no durable progress must not be repeated blindly.

When canonical retained recovery has committed success but reports incomplete
cleanup, use the existing `publish --result ORIGINAL_RESULT.json
--reconstruct-contexts --trusted-context-root ORIGINAL_POLICY` route with all the
same repository/base/head, trusted diff, original policy and broker options. The
file must contain the exact original result, obtained from the original artifact
or independently integrity-verified retained parts; a baseline or edited result
cannot substitute. The normal accepted-result guard remains enabled. This route
avoids repeated retained-result discovery, without removing fresh publication or
mutation fences. In the same captured state it used 58 calls and resolved one old
thread, preserving the terminal ledger and every current obligation. Five-step
terminal controls also demonstrated one old-thread resolution per invocation.
These finite observations do not establish completion of every old thread or
authorize new inference, current-thread resolution or approval.

Missing, duplicate, malformed, foreign-owned, foreign-PR, stale-head, mismatching
configuration or digest parts refuse. A failed/interrupted initial write can
leave an unconfirmed durable phase. A fresh owner reconciles complete owned parts;
an incomplete or absent set cannot be blindly recreated. This hold requires
operator investigation. If the exact original result and trusted contexts are
available, the existing explicit `operation_entry: disabled` rollback can publish
those original bytes through ordinary validation; it is not a provider retry or
permission bypass. Do not change this switch merely to clear a genuine finding.

This serial crash fence is not an atomic distributed lock. Concurrent REST comment
writers remain an unqualified condition. Large feedback histories can still exhaust
the shared 60-second, 60 ordinary / 64 total dispatch budget and remain blocked.
No silent truncation, cap raise or older clean-review fallback is permitted.

Recorded maintainer decisions can outgrow a legacy review even when its original
inventory fits. See [capacity-safe command reconciliation](command-publication-capacity.md)
for whole-frame reservation, guarded generated-reference fitting and persistent
capacity holds. Durable decisions remain bound to their original exact head;
source repair does not activate a deployed runtime or authorize fresh analysis.

The older `--recover-from` diagnostics-artifact route retains its existing legacy
contract. It rejects the new publication-profile settings instead of silently
ignoring them. Use `--result` with the exact original result or
`--recover-retained` with independent transaction context for the complete profile.

## PR #316 incident boundary

At head `f59edcc89d54a72ca12d97300e69463417646bfd`, the provider run completed but
the deployed publisher rejected its inventory. No full result parts or diagnostics
artifact survived. The 77-record baseline contains identities, not all original
explanations, and cannot reconstruct the result. This repair prevents subsequent
loss; it cannot recover that old payload. Do not rerun its provider merely to test
publication. Verify the repaired installed path first and request any separate
future inference authorization afterward.

## Acceptance matrix

| Issue / condition | Default code seam | Executable evidence | Scope / remaining limit |
| --- | --- | --- | --- |
| #238, #246: 65/77/100/250 findings | ReviewPublisher gated v1 writer and index reader | `test_bounded_v1_publication.CompleteInventoryPublicationTests` | Full identities/prose for compact inventories; byte/path/future-assessment limits remain. |
| #238: oversized individual/prose inventory | Complete-profile preflight | Same contract: maximum individual prose, overflow and no-mutation cases | Invalid individual values remain invalid; no truncation. |
| #246, PR #316: publication failure loses result | GitHubApplication plus existing ResultPartStore | `test_publication_recovery.PublicationRecoveryTests`; `test_default_publication_cli_journey` | Actual default 77-result checkpoint, serialized ledger and fresh caller. Larger operator baseline persistence is a separate prerequisite. |
| #240, #246: interruption or unknown write | Durable unconfirmed phase before first part POST; complete-owned readback | Recovery crash-before/crash-after and lost-POST methods | Missing uncertain parts hold; no blind POST retry. Distributed atomic writer exclusion remains unqualified. |
| #246: idempotent review replay | Exact transaction/content marker plus authenticated full prose readback | Bounded publisher lost-ack, body/inline replay and unrelated-root cases | Old transaction-hash or omitted-prose roots cannot be silently adopted. |
| #267: read/deadline bounds | Shared publication transport and EvidenceReadBudget | Publication recovery budget/deadline methods and public finalizer charge test | No budget refill; long histories can remain blocked. This does not qualify the earlier whole-provider P1 dispatch profile. |
| #246: accepted decisions / stale authority | Existing acceptance guard plus independent transaction validation | `test_checkpoint_publication_acceptance` | Original accepted content survives checkpoint-envelope changes; changed content/context/scope still refuses. |
| #238, #246: native replay approval authority | Complete native index plus actual persisted eligibility | `test_publication_recovery.NativeReplayAuthorityTests` and installed selection | Missing, malformed or unbound authority refuses with a non-passing check. Persisted approval opt-out cannot be reconstructed away; valid retained authority and current obligations keep their existing policy. |
| Caller/configuration compatibility | Configuration parser, hosted plan, both workflow publication lanes, actual CLI | `test_hosted_plan`, `test_publication_cli_recovery`, `test_default_publication_cli_journey` | Default emission stays 64 pending separately authorized reader-first rollout. |
| Installed / post-package workflows | Extracted sdist contract against installed engine and adapter | `dist_safe/test_default_publication_recovery_contract.py`; published CLI `lifecycle.py` | Runtime imports must match installed artifacts; no checkout source fallback. |
| #313, #314, provider/configuration #115–123/#146 | Separate baseline, provider-deadline and provider-contract changes | Separate PR acceptance matrices | This focused repair does not claim deployment or live-provider qualification. |

These tests use synthetic GitHub transport and an actual local durable ledger.
They prove the supported deterministic path, not an authorized production GitHub
mutation, deployed reader rollout, real-provider coverage, or all concurrency
interleavings. None of the broader issues is closed merely because this table or
component code exists.
