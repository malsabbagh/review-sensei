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
Read-only bootstrap remains on GitHub and validates one 1-128-character label
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
software remain the operator's responsibility. Label shape validation does not
prove that a runner exists or its operating system is compatible.

## Alternatives considered

Changing wrapper `runs-on` cannot select reusable jobs. Reading a downstream
variable implicitly would couple infrastructure policy to every caller. Array
and group inputs would enlarge the contract without a demonstrated need.

## Validation

Tests execute the actual bootstrap script for defaults, valid bounded labels,
invalid shapes and injection-like values, and assert the three consumers,
GitHub bootstrap, unchanged local lane and generated caller defaults. Combined
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
