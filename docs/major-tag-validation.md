# Major-channel validation

`major-tag-validation.yml` validates a public workflow channel without moving
tags, publishing packages, invoking providers or writing to PRs. It reuses the
exact installed-PyPI qualifier from `published-cli.yml`.

## Admission and integrity

The `push.tags: ["v*"]` filter is a glob. The gate matches the **whole name**
against `v(?:0|[1-9][0-9]*)`: `v5`, `v6`, `v123` qualify; immutable semver,
prerelease, uppercase, suffixed, leading-zero and deleted tags do not.
Creation and force-update both produce push events. Promote one channel at a
time: GitHub can omit events for pushes containing more than three tags.

The gate uses the existing `release_docs.py` GitHub-verified annotated-signature
policy. Lightweight and nested tags fail. The major object's peeled source
must match the event. Its exact stable immutable release tag, package/site/npm
manifests, published GitHub release, authoritative release binding, asset
digests and successful source-bound Release run must agree. Channel numbers
are independent of package versions: `v5` can select `0.6.21`.

The wheel is downloaded by a validated numeric GitHub asset id, hashed and
verified with the existing `gh attestation verify` policy: repository, Release
workflow, immutable tag ref, source digest and GitHub-hosted runner. The later
PyPI wheel must match that digest **before installation**.

Tagged workflow files are parsed as bounded data and never executed by this
validator. Selected checks cover caller/reusable inputs, explicit optional
provider secrets, repository read permissions, Action pins, four exact-version
installation lanes, source-based version resolution and two publication
metadata-token paths. Duplicate YAML keys, aliases and executable tags fail.
The existing `job.workflow_ref`/`job.workflow_sha` binding is preserved.
Actionlint/current docs omit these properties, but an observed downstream run
resolved the correct callee tag object and installed 0.6.21. This warning is
retained as a diagnostic, not a proven runtime failure. Substituting caller
`github.workflow_*` fails the identity contract.

## Installed CLI and stale promotions

Linux/macOS Python 3.11/3.14 lanes install only the exact public wheel in fresh
environments. Credentials, pip overrides and source import paths are removed;
imports must resolve inside that environment's `site-packages`. The existing
five suites exercise config precedence, machine output, exit status, limits,
error paths, fixture/loopback transport/cancellation and mocked operation safety.
Zero/inconsistent receipts, timeouts, nonzero exit and failed cases fail jobs.
Sanitized artifacts are retained on failure; environments and binaries are not.

All lanes freeze validation code to one protected default-branch checkout SHA.
Same-channel concurrency cancels older runs. The final job requires every CLI
lane to pass and rechecks the exact major object/source, immutable release and
asset inventory. Green evidence names an exact object/source; the tag can move
again after the last read. A later head is never silently substituted.

## Actual promotion mechanism

Tag-push workflows run from the tagged revision. A new workflow on main is
absent from already immutable releases: it cannot retroactively validate the
completed `v5 -> 0.6.21` move or a rollback to a historical target lacking it.

The current promotion uses a local signed Git push with an old-object lease,
the existing `gh` **user** credential helper and the existing SSH agent signer.
It does not use an Actions `GITHUB_TOKEN`, so recursion suppression does not
apply. No annotated push payload was captured. If GitHub exposes the tag object,
the gate binds it; a peeled-only event binds the source and freezes the observed
object, but cannot identify a same-source re-sign completed before admission.

For historical targets, exact object binding or `GITHUB_TOKEN` suppression, use
the default-branch dispatch bridge with values from the promotion receipt:

```sh
python scripts/dispatch_major_tag_validation.py \
  --channel v5 --tag-object EXACT_SIGNED_OBJECT --source-sha EXACT_PEELED_SOURCE
```

This only reads and prints the exact command. A separately authorized
post-promotion operation adds `--dispatch`. The helper rechecks both identities
and never moves tags or creates credentials. `workflow_dispatch` is exempt from
token recursion suppression, but needs existing caller Actions authorization.
This read-only workflow does not add permissions or credential scopes. Missing
workflow/rejected dispatch is a blocker; dispatch acceptance is not completion.

See GitHub's [event/recursion rules](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows),
[manual workflow rules](https://docs.github.com/actions/managing-workflow-runs/manually-running-a-workflow)
and [context reference](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts).

## Acceptance evidence and limitations

Local read-only acceptance verified signed `v5`/`0.6.21` source
`90287a9d847d07668327980684beab85c3e19d39` and the actual wheel attestation for
`58f9ed48e8df2434c2136ba9e57c260e606fd550e396cd422ee2efd1d4d4ef31`.
That exact PyPI wheel passed **1,082** portable checks on macOS ARM64/Python3.14.
These are installed-process/seam checks with fixtures, not live models or writes.
An observed downstream bootstrap resolved the exact signed callee
object/source and installed 0.6.21. The hosted lane failed later with
`action_required: paused`; no publication success or command-lane execution
is claimed. The static context warning did not cause that runtime failure.

`major-tag-validation-ci.yml` runs the event/gate contract tests and the actual
installed harness on Linux/macOS for changes to this validator. Its immutable
regression baseline is 0.6.21 with the digest above; it does not substitute that
baseline for the newly promoted version in the major-tag workflow. A future
project version bump does not require an unpublished wheel for these CI checks.

No new tag, dispatch, release or paid provider call was performed. Before merge,
the new tag-push trigger cannot be proven live. Future authorized execution must
establish the promotion's four CLI lanes. This does not prove native/npm/Windows limits,
broker/OIDC grants, live writes, real provider maxima, every permutation or a fix
for the existing giant-line evaluation slowdown.
