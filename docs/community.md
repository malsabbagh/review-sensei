# Community and governance

ReviewSensei is an open-source-first, provider-neutral project. The engine runs
in a user's repository, primarily through GitHub Actions, and does not operate
a hosted review backend. Community participation must preserve the project's
security, privacy, and validated-output boundaries.

## Governance

The maintainer is responsible for project direction, security boundaries,
release readiness, and final acceptance. Contributors and reviewers discuss
proposals openly in issues and pull requests, following the [Code of
Conduct](../CODE_OF_CONDUCT.md). Maintainers may moderate content, request
changes, defer proposals, or close duplicates and out-of-scope requests while
explaining the rationale when practical.

The repository's architecture and decision records are the durable source of
technical policy. See [`architecture.md`](architecture.md) and the
[`docs/adr/`](adr/) index. Changes to public contracts, data handling,
authentication, persistence, deployment, or dependency direction need an ADR;
documentation-only changes generally do not.

## Issue triage

Use the [bug](../.github/ISSUE_TEMPLATE/bug_report.yml),
[feature](../.github/ISSUE_TEMPLATE/feature_request.yml), and
[documentation](../.github/ISSUE_TEMPLATE/documentation.yml) forms. Maintainers
review new issues, apply labels, identify duplicates, request a minimal
sanitized reproduction, and record dependencies or scope decisions. Security
vulnerabilities are not triaged in public issues; follow
[`SECURITY.md`](../SECURITY.md).

Proposed labels include:

* `bug` — reproducible incorrect behavior
* `enhancement` — proposed user-facing capability
* `documentation` — documentation gap or correction
* `triage` — awaiting maintainer classification or more information
* `good first issue` — bounded work suitable for a new contributor
* `help wanted` — maintainer welcomes implementation help
* `blocked` — dependent on a decision, issue, or external prerequisite

Proposed repository topics include `python`, `code-review`, `github-actions`,
`ollama`, and `open-source`. These labels and topics are suggestions for
discoverability and consistent triage, not promises that every issue will
receive a particular priority or timeline.

## Support boundaries

Use [SUPPORT.md](../SUPPORT.md) for questions and troubleshooting. Public
reports must contain only sanitized details: never post credentials, private
source, private diffs, personal data, or raw provider responses. Support is
best-effort and covers the documented open-source engine, CLI, workflow, and
provider seams. Maintainers do not run a hosted backend, inspect private
repositories, or provide a private incident-response service through public
issues.

## Roadmap

The roadmap is tracked through [GitHub Issues](https://github.com/malsabbagh/review-sensei/issues),
including priorities, dependencies, and future milestones. Issue status is
more current than an informal roadmap document; proposed work is not a promise
of dates or acceptance.

## Releases

Maintainers own version selection, changelog updates, signed tags, package
publication, release attestations, and rollback or yank decisions. The
step-by-step policy is [`docs/releasing.md`](releasing.md). Contributors should
not push tags, publish packages, or invoke release workflows without explicit
maintainer delegation.

## Maintainer decision-making

Maintainer decisions weigh user impact, reproducible evidence, security and
privacy, compatibility, maintenance cost, and the open-source-first scope.
Public discussion informs decisions, but maintainers retain merge, release,
security, and final-scope authority. Material architectural choices are
recorded in ADRs, with validation and rollback guidance.

## Optional GitHub Discussions

If maintainers enable GitHub Discussions, it can host open-ended questions,
ideas, and community introductions that do not need an issue's acceptance
criteria. Reproducible bugs, actionable feature requests, documentation gaps,
and security reports still belong in their issue or private-security routes.
Discussion threads should follow the Code of Conduct and the same no-private-data
rule as issues.

GitHub labels, repository topics, and Discussions are maintainer settings. The
proposed labels and optional Discussions guidance above are documentation only;
this change does not apply those settings.
