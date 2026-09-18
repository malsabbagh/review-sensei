# ADR 0033: Protection drift and deterministic findings gates

- Status: Proposed
- Date: 2026-09-13
- Issues: #26, #32

## Decision

Keep repository settings declarative and auditable in
`.github/protection-policy.json`, with a read-only checker that compares an
administrator-captured ruleset response. The checker never writes GitHub
settings and distinguishes configuration evidence from behavioral merge tests.

Run CodeQL for Python and JavaScript/TypeScript (the Cloudflare Worker and npm
launcher) with pinned actions and no secrets on pull requests. Because code
scanning upload is not an available enforcement surface for this repository,
CodeQL writes SARIF to an artifact and `scripts/check_codeql_findings.py` is the
deterministic gate. Warning/error findings fail unless their stable fingerprint
is listed in the reviewed baseline with a rule, location, rationale, owner, and
expiry. SARIF report collection is deterministic, bounded, and does not follow
symlinks; malformed or non-finite severity values and malformed
`partialFingerprints` fail closed. Expiry on the current date is treated as
expired. The required-checks aggregate includes this gate through the CodeQL
job.

## Rationale and limits

The baseline is intentionally empty and exceptions expire after 30 days; any
addition is a reviewed change. Each CodeQL matrix job enforces coverage for
its language from trusted SARIF run metadata. The checker permits the action
to emit multiple files or runs for one language, while rejecting unexpected or
missing languages. The `required-checks` aggregate job depends on the complete
matrix, so both language jobs must pass before the single branch-protection
status is green; a missing analysis cannot appear as zero findings. The
pull-request CI check validates the policy document only;
the manual ruleset-readback workflow compares an administrator-captured API
response and reports live drift. Maintainers must still run disposable
behavioral tests before applying settings.

## Amendment (2026-09-16) — issue #32 fixture proof

Keep ADR 0033 Proposed until a maintainer accepts it. This amendment records
the remaining findings-gate evidence that does not require GitHub code-scanning
upload:

- CI analyzes Python and JavaScript/TypeScript with pinned CodeQL actions and
  `contents: read` / `actions: read` only. Fork pull requests use `pull_request`,
  not `pull_request_target`, and the CodeQL job never references `secrets.*`.
- Enforcement remains the local SARIF gate. Upload stays `never` and is not
  treated as a merge control.
- Committed fixtures under `tests/fixtures/codeql/` prove three outcomes in the
  `Schemas and Action pins` job and in unit tests: clean reports pass; a seeded
  warning fails the gate; malformed SARIF fails closed. The `Required checks`
  aggregate already depends on the live CodeQL matrix, so a gate failure fails
  the merge-policy job. Live GitHub ruleset attachment of that aggregate is
  still operator evidence under #26.

## Amendment (2026-09-16) — issue #26 tag ruleset readback

Keep ADR 0033 Proposed until a maintainer accepts it. This amendment records
tag-ruleset comparison added to the read-only checker:

- `scripts/check_protection_policy.py` compares captured immutable
  semantic-version tag rulesets and the separately protected movable `v4`
  channel. GitHub fnmatch include patterns are translated conservatively from
  `tags.immutable_pattern`; the checker never writes rulesets.
- `v4` promotions reuse `.publication/publication-ledger.jsonl` with a
  validated `v4_promotion` JSONL record. That file is not a hosted ledger
  service.
- Local `--policy` checks remain configuration evidence only. Live ruleset
  application, disposable merge tests, and production tag moves stay
  maintainer-operator evidence for #26.

## Amendment - public channel `v5`

The current generated-caller write channel is `v5`. Protect `refs/tags/v5`
with the same channel-ruleset shape as historical `v4`. Keep `v4` protected
while the broker still accepts it. The checked-in protection policy now lists
`movable_channels: ["v4", "v5"]`. The `v4_promotion` ledger record type name
remains the original schema until that contract is versioned.
