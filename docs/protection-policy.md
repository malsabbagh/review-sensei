# Protection policy and drift runbook

The checked-in [policy contract](../.github/protection-policy.json) describes
the controls that must be applied to `main` and release tags. It is deliberately
not a GitHub settings payload: repository administrators must apply settings in
GitHub and retain the authoritative API response as evidence.

## Required controls

- `main` requires pull requests, one approval, code-owner review, stale-review
  dismissal, conversation resolution, latest-push approval, and the `Required
  checks` status with strict/up-to-date semantics.
- Bypass is limited to a named maintainer for pull-request recovery. Do not add
  a blanket `always` bypass or a bot bypass; record any recovery use.
- Immutable semantic version tags (`vMAJOR.MINOR.PATCH`) cannot be deleted or
  replaced. `v4` is the sole movable operator-managed setup channel, and every
  promotion is recorded in `.publication/publication-ledger.jsonl`.

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
