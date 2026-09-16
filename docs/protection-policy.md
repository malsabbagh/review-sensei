# Protection policy and drift runbook

The checked-in [policy contract](../.github/protection-policy.json) describes
the controls that must be applied to `main` and release tags. It is deliberately
not a GitHub settings payload: repository administrators must apply settings in
GitHub and retain the authoritative API response as evidence.

## Required controls

- `main` requires pull requests, one approval, code-owner review, stale-review
  dismissal, conversation resolution, latest-push approval, and the `Required
  checks` status with strict/up-to-date semantics. That aggregate includes the
  CodeQL findings gate for Python and JavaScript/TypeScript; a seeded or
  malformed SARIF report fails the gate, and a failed CodeQL job fails
  `Required checks`. Live ruleset attachment of the aggregate remains
  administrator evidence, not a CI inference.
- Bypass is limited to the repository owner `@malsabbagh` (user ID `13791232`)
  for pull-request recovery (`max_bypass_actors: 1`). The identity is explicit
  so a future maintainer or collaborator cannot inherit break-glass authority
  accidentally. Revalidate the user ID before copying this repository-specific
  contract. Do not add a blanket `always` bypass or a bot bypass; record any
  recovery use.
- Immutable semantic version tags (`vMAJOR.MINOR.PATCH`) cannot be deleted or
  replaced. `v4` is the sole movable operator-managed setup channel, and every
  promotion is recorded in `.publication/publication-ledger.jsonl`.

## GitHub fnmatch versus policy regex

The checked-in `tags.immutable_pattern` is an anchored regular expression
(`^v[0-9]+\.[0-9]+\.[0-9]+$`) for that semantic-version set. GitHub rulesets do
not accept that regex. Their `conditions.ref_name.include` field uses fnmatch
and stores the full ref (`refs/tags/...`), even when the settings UI shows a
shorter tag pattern.

The only accepted conservative translation for immutable tags is:

```text
refs/tags/v[0-9]*.[0-9]*.[0-9]*
```

That fnmatch is broader than the policy regex: `[0-9]*` is one digit plus a
glob-star, so names such as `v1.2.3-rc1` or `v1x.2.3` can match GitHub while
failing the regex. Do not "tighten" this by switching to `refs/tags/v*`,
`refs/tags/*`, or `~ALL`; those patterns fail the readback. Behavioral tests
in the disposable-validation checklist below remain required because
configuration readback cannot prove GitHub's matcher equals the regex.

The movable `v4` channel is a separately protected, intentionally updatable
tag. Its accepted include pattern is the exact ref `refs/tags/v4`. That
ruleset must restrict deletion and require signed commits, and it must **not**
include GitHub's `update` rule. Authorized operators with write access can
move the tag; a blanket `always` bypass is not an accepted way to make `v4`
movable. Unauthorized deletion must still be rejected.

## Configuration evidence versus behavioral tests

`python scripts/check_protection_policy.py --policy .github/protection-policy.json`
is **configuration evidence only**. It never talks to GitHub, never attempts a
merge, and is not a merge test. A passing local check does not prove that a
failing PR is blocked or that a tag push is rejected.

Readback comparison (`--readback`, `--tag-readback`, `--channel-readback`, or
`--readback-dir`) is also configuration evidence: it checks that a captured
ruleset API response matches the contract. It is still not a merge test.

Behavioral evidence is the numbered disposable-validation checklist below.
Use a disposable fork or repository. Never mutate production tags or rulesets
during validation. Applying live rulesets remains maintainer-operator work;
this repository's code and CI cannot themselves create or update GitHub
rulesets.

## Read-only verification

Validate the contract in any checkout:

```sh
python scripts/check_protection_policy.py \
  --policy .github/protection-policy.json \
  --promotion-ledger .publication/publication-ledger.jsonl
```

An administrator can capture ruleset responses and compare them without making
changes (replace `ID` values with the live ruleset ids):

```sh
GITHUB_TOKEN='' gh api repos/malsabbagh/review-sensei/rulesets/BRANCH_ID \
  > /tmp/reviewsensei-branch-ruleset.json
GITHUB_TOKEN='' gh api repos/malsabbagh/review-sensei/rulesets/TAG_ID \
  > /tmp/reviewsensei-tag-ruleset.json
GITHUB_TOKEN='' gh api repos/malsabbagh/review-sensei/rulesets/CHANNEL_ID \
  > /tmp/reviewsensei-v4-ruleset.json
python scripts/check_protection_policy.py \
  --policy .github/protection-policy.json \
  --readback /tmp/reviewsensei-branch-ruleset.json \
  --tag-readback /tmp/reviewsensei-tag-ruleset.json \
  --channel-readback /tmp/reviewsensei-v4-ruleset.json
```

`--readback` remains the branch-ruleset comparison and can be used alone.
Tag comparison fails closed unless both the immutable tag ruleset and the `v4`
channel ruleset are provided. Equivalent captures may be placed in one
directory:

```sh
python scripts/check_protection_policy.py \
  --policy .github/protection-policy.json \
  --readback-dir /tmp/reviewsensei-rulesets
```

Sanitized templates for those JSON files live under
`tests/fixtures/protection/` (no tokens). Capture `GET
/repos/{owner}/{repo}/rulesets/{id}` objects, not the summary list endpoint.

The check fails closed when a response is malformed, the target ref or
pull-request/status-check parameters drift, `Required checks` is missing, tag
protection is missing, deletion is allowed, unsigned replacement is allowed,
a bypass actor is missing, unexpected, or has `bypass_mode: always` (or another
unsupported mode), or GitHub include patterns are not the accepted fnmatch
translation.

The manual `Protection policy readback` workflow performs the same capture and
comparison from a controlled runner. Configure the repository secret
`REVIEWSENSEI_RULESET_READ_TOKEN` with a narrowly scoped GitHub App or
fine-grained token that can read this ruleset (the API only includes
`bypass_actors` when the caller can view the ruleset details), then dispatch the
workflow with the active branch ruleset ID and, when tag rulesets exist, the
immutable tag and `v4` channel IDs. The workflow performs a GET capability
check before capture and fails closed when the secret or ID is missing. It
rejects classic OAuth credentials advertising broad `repo`, `admin:*`,
`workflow`, or `delete_repo` scopes; App and fine-grained tokens normally omit
the legacy `X-OAuth-Scopes` header, so their ruleset GET is the authoritative
permission check. Provision this as a dedicated readback credential and never
reuse a deployment credential. Rotate or revoke it independently if the runner
boundary is compromised.

## Sanitized readback evidence template

Attach captured ruleset JSON as issue or pull-request evidence only after
sanitizing it. Keep the fields the checker compares; drop secrets and
request metadata.

1. Copy the `GET /repos/{owner}/{repo}/rulesets/{id}` body for each of the
   branch, immutable-tag, and `v4` channel rulesets into separate files named
   like `tests/fixtures/protection/readback-dir/` (`branch-ruleset.json`,
   `immutable-tag-ruleset.json`, `v4-channel-ruleset.json`).
2. Delete `Authorization`, cookie, and `X-OAuth-*` headers if you saved a raw
   HTTP transcript. Never commit `GITHUB_TOKEN`, App private keys, or
   `REVIEWSENSEI_RULESET_READ_TOKEN`.
3. Keep `id`, `name`, `target`, `enforcement`, `conditions.ref_name`, `rules`,
   and `bypass_actors` (`actor_id`, `actor_type`, `bypass_mode` only). Numeric
   actor IDs are public GitHub identities, not credentials.
4. Redact `node_id` / `_links` / webhook URLs if they appear. Do not include
   the list-endpoint summary that omits `rules`; that capture cannot prove
   protection.
5. Run the checker against the sanitized files and attach both the JSON and
   the command output. Label the result **configuration evidence**, not a
   merge test.

## v4 promotion audit record

Publication provenance and `v4` promotions share
`.publication/publication-ledger.jsonl`. Do not invent a hosted ledger.
A `v4` promotion line is a JSON object with `record_type` `v4_promotion`,
`tag` `v4`, `previous_sha`, `new_sha` (distinct 40-character lowercase git
SHAs), a non-secret `operator` identity, an RFC 3339 `timestamp`, and a
`reason`. The helper `build_v4_promotion_entry` /
`append_v4_promotion_entry` in `scripts/check_protection_policy.py` validates
that schema before append. `--promotion-ledger` rejects missing previous/new
SHA, equal SHAs, and malformed JSONL lines, while still accepting existing
public-sync provenance entries.

After an authorized `v4` move, append a promotion line in the same commit
that records the operator action, then re-run the checker with
`--promotion-ledger`.

## Disposable-validation checklist

These steps are behavioral evidence. Perform them on a disposable fork or
throwaway repository that copies the intended rulesets. Do not force-push,
delete, or retarget production `vX.Y.Z` tags or the production `v4` tag.

1. Failing or missing `Required checks` cannot merge via the ordinary
   contributor path. Open a disposable PR whose required aggregate is red or
   absent and confirm GitHub refuses merge for a non-bypass actor.
2. A correctly approved, passing PR can merge. Use a second disposable PR
   with green `Required checks`, one approving review, code-owner review,
   resolved conversations, and an up-to-date head.
3. Owner-authored and automation-authored PR paths do not deadlock. Confirm
   the owner can still land a self-authored change through the documented
   pull-request bypass (not `always`), and that publication/CI automation
   does not require a new bot bypass actor.
4. Unauthorized release-tag changes are rejected. From an account without
   bypass, attempt to delete or move a disposable `v0.0.0`-style tag covered
   by the immutable pattern, and confirm GitHub denies the update.
5. An authorized `v4` promotion remains possible and writes a ledger record.
   On the disposable repository, move a disposable channel tag, then write a
   `v4_promotion` line with previous SHA, new SHA, operator identity,
   timestamp, and reason. Confirm the checker accepts the ledger and that
   unauthorized deletion of that channel tag is still denied.

Record pass/fail, actor, repository, and UTC time for each item. That packet
is the behavioral evidence for issue #26; the checker output is not a
substitute.

## Maintainer-operator actions still required

This tracking issue and its pull requests must not mutate live GitHub
repository settings. A maintainer with repository administration must still:

- Apply the required status-check, review, conversation, last-push, immutable
  tag, and `v4` channel rules in repository settings (or via an
  administrator-owned API client outside this checkout).
- Capture sanitized readback JSON for all three rulesets and retain it with
  the issue evidence.
- Execute the disposable-validation checklist above.
- Perform any real `v4` promotion on the public channel and append the ledger
  record.

CI and `check_protection_policy.py` remain read-only relative to GitHub.
