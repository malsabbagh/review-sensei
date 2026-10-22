# ADR 0062 - Allow a trusted caller to select one hosted runner label

Status: Proposed
Date: 2026-10-06
Owners/Reviewers: Maintainers
Approved by: not applicable
GitHub Issue: release request tracked in PR #212
GitHub PR: [#212](https://github.com/malsabbagh/review-sensei/pull/212)

## Context

Downstream callers cannot select their existing Ubicloud runners while the
public reusable workflow hardcodes GitHub runners. Changing a caller's wrapper
runner alone does not affect the jobs inside the reusable workflow.

## Decision and scope

Add optional string input `hosted_runner`, defaulting to `ubuntu-latest`.
Read-only bootstrap uses that input with the same empty-input default and
validates one 1-128-character label in its first step
starting with a letter or digit and containing only letters, digits, dots,
underscores and hyphens; empty input uses the default. Its validated output
selects authoritative preflight, command and hosted-provider jobs.

The operator must choose an Ubuntu-compatible trusted Linux runner. Arrays and
runner groups are unsupported. Local Ollama retains its existing label array.
Provider configuration, App permissions, broker capabilities, OIDC workflow
identity and this repository's CI routing are unchanged. Generated callers do
not set the optional input; operators opt trusted callers in explicitly.

## Consequences

Existing callers retain GitHub execution. An opted-in runner receives the same
trusted workflow and existing named credentials, so runner trust and compatible
software remain the operator's responsibility. GitHub schedules bootstrap before
its first step can validate the input, so a malformed or unavailable label may
be rejected or queued by the scheduler. Downstream jobs use only the validated
output. Label shape validation does not prove that a runner exists or its
operating system is compatible. Bootstrap remains read-only, freezes policy
from the trusted base, and never materializes provider credentials.

## Alternatives considered

Changing wrapper `runs-on` cannot select reusable jobs. Reading a downstream
variable implicitly would couple infrastructure policy to every caller. Array
and group inputs would enlarge the contract without a demonstrated need.

## Validation

Tests execute the actual bootstrap script for defaults, valid bounded labels,
invalid shapes and injection-like values, and assert input-based bootstrap
scheduling, validation before work, the three validated-output consumers,
read-only permissions, unchanged local lane and generated caller defaults. Combined
full release tests and CI validate the workflow contract. Actual Ubicloud
execution is a separate rollout check.

## Rollout and rollback

Publish the reviewed package/workflow cutoff and promote approved `v5` with its
broker identity before adding this input to tag-pinned callers. Older workflows
reject unknown inputs. Verify an opted-in runner, a default caller and local
Ollama. Roll back callers by omitting the input; remove it before rolling the
channel back to a workflow that does not declare it. See `docs/releasing.md`.

## Follow-up work

Apply deferred opted-in caller arguments only after the released `v5` accepts
them. Record live routing evidence without credentials. Keep this ADR Proposed
until maintainer review.

## 2026-10-22 bootstrap routing repair

The original implementation kept bootstrap on `ubuntu-latest`. SessionVault
and SiteVault callers passed `ubicloud-standard-2`, but GitHub-hosted billing
rejected bootstrap before it could select the downstream runner. This amendment
applies the existing trusted caller runner contract to bootstrap as well. It
adds no permissions or provider/policy inputs. Existing signed releases remain
immutable; ship the repair through a reviewed release and qualified `v5`
promotion, then retain actual downstream bootstrap routing evidence.
