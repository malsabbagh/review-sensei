# Protection policy and drift runbook

The checked-in [policy contract](../.github/protection-policy.json) describes
the controls that must be applied to `main` and release tags. It is deliberately
not a GitHub settings payload: repository administrators must apply settings in
GitHub and retain the authoritative API response as evidence.

## Required controls

- `main` requires pull requests, one approval, code-owner review, stale-review
  dismissal, conversation resolution, latest-push approval, and the `Required
  checks` status with strict/up-to-date semantics.
- Bypass is limited to the repository owner `@malsabbagh` (user ID `13791232`)
  for pull-request recovery (`max_bypass_actors: 1`). The identity is explicit
  so a future maintainer or collaborator cannot inherit break-glass authority
  accidentally. Revalidate the user ID before copying this repository-specific
  contract. Do not add a blanket `always` bypass or a bot bypass; record any
  recovery use.
- Immutable semantic version tags (`vMAJOR.MINOR.PATCH`) cannot be deleted or
  replaced. `v4` is the sole movable operator-managed setup channel, and every
  promotion is recorded in `.publication/publication-ledger.jsonl`.

The checked-in `tags.immutable_pattern` is an anchored regular expression for
that semantic-version set. GitHub ruleset pattern syntax is configured
separately by an administrator and must be translated conservatively, then
verified with the readback and behavioral evidence described below.

## Read-only verification

Validate the contract in any checkout:

```sh
python scripts/check_protection_policy.py \
  --policy .github/protection-policy.json
```

An administrator can capture a ruleset response and compare it without making
changes (replace `ID` with the ruleset id):

```sh
GITHUB_TOKEN='' gh api repos/malsabbagh/review-sensei/rulesets/ID \
  > /tmp/reviewsensei-ruleset.json
python scripts/check_protection_policy.py \
  --policy .github/protection-policy.json \
  --readback /tmp/reviewsensei-ruleset.json
```

The manual `Protection policy readback` workflow performs the same capture and
comparison from a controlled runner. Configure the repository secret
`REVIEWSENSEI_RULESET_READ_TOKEN` with a narrowly scoped GitHub App or
fine-grained token that can read this ruleset (the API only includes
`bypass_actors` when the caller can view the ruleset details), then dispatch the
workflow with the active ruleset ID. The workflow runs only a GET request and
fails closed when the secret or ID is missing. Provision this as a dedicated
readback credential and enforce its scope when creating the secret; the
workflow cannot introspect or reduce permissions on an opaque token. Never
reuse a deployment credential, and rotate or revoke the readback credential
independently if the runner boundary is compromised.

The check fails closed when the response is malformed, the target branch or
pull-request/status-check parameters drift, `Required checks` is missing, or a
bypass actor is missing, unexpected, or has `bypass_mode: always` (or another
unsupported mode). A passing local check is configuration evidence only; it is
not a behavioral merge test. Use a disposable fork or repository for
failing-check and unauthorized-tag tests, and never mutate production tags or
rulesets during validation.

## Maintainer actions still required

Apply the required status-check, review, conversation, last-push, and tag rules
in repository settings; then rerun the readback command and retain the JSON
response with the PR evidence. Confirm owner-authored and automation-authored
PRs remain mergeable without granting broad bypass permissions.
