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
addition is a reviewed change. Each CodeQL matrix job requires exactly one
metadata-labelled report for its language, and the aggregate required check
requires both language jobs to pass, preventing a missing analysis from
appearing as zero findings. A read-only policy check in CI validates the
contract but cannot prove GitHub's live ruleset; maintainers must capture and
compare the authoritative API response and run disposable behavioral tests
before applying settings.
