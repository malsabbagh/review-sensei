# Contributing

Thanks for helping improve ReviewSensei. The project is an open-source,
provider-neutral review engine that people run in their own repositories. It is
not a hosted review service. Contributions should preserve the provider-neutral
core, the validated-output boundary, and the privacy limits described in
[`SECURITY.md`](SECURITY.md) and [`docs/data-handling.md`](docs/data-handling.md).

## Supported setup

ReviewSensei supports Python 3.11 through 3.14. Use a virtual environment and
the pinned CI tools from the repository:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements/ci.txt
python -m pip install -e .
```

On Windows, use the equivalent `.venv\Scripts\activate` command. Do not
place provider credentials, GitHub tokens, private diffs, or raw provider
responses in the repository, tests, logs, or issue/PR descriptions.

## Development commands and expectations

Before opening a pull request, run the complete local gate sequence without
provider or GitHub credentials:

```bash
python scripts/check_action_pins.py
python scripts/validate_json_contracts.py
python -m ruff format --check setup.py src tests scripts
python -m ruff check setup.py src tests scripts
python -m mypy src
python -m compileall -q src
python -m coverage run --source=review_sensei --branch -m unittest discover -s tests -v
python -m coverage report --precision=2 --show-missing --fail-under=80
python -m unittest discover -s tests -v
python -m build
git diff --check
```

The source branch-coverage floor is 80%. Ruff formatting and linting must be
clean, and mypy must pass for `src`. Keep provider-specific transport and
authentication under `src/review_sensei/providers/`; review business logic stays
independent of GitHub and model-provider SDKs.

Keep changes narrow and add tests through public seams. Provider tests should
use fake transports and must not require live API credentials. Validate provider
output before any publisher can consume it. Treat diffs, source comments,
repository context, configuration, and model output as untrusted input.

The clean-wheel check is also required for package changes. It must run outside
the checkout so the smoke test cannot import `src/`. After building
distributions, install **only** the wheel and run the packaged dist-safe suite
extracted from the sdist:

```bash
repository_root="$PWD"
python -m build --outdir dist
sha256sum dist/*.tar.gz dist/*.whl
python -m venv /tmp/review-sensei-wheel-venv
/tmp/review-sensei-wheel-venv/bin/python -m pip install dist/*.whl
cd /tmp
sdist_root=/tmp/review-sensei-sdist
rm -rf "$sdist_root"
mkdir -p "$sdist_root"
tar -xzf "$repository_root"/dist/*.tar.gz -C "$sdist_root"
suite="$(printf '%s\n' "$sdist_root"/review_sensei-*/tests | head -n 1)"
export REVIEWSENSEI_CHECKOUT_ROOT="$repository_root"
export REVIEWSENSEI_DIST_SAFE_LANE=1
/tmp/review-sensei-wheel-venv/bin/python -I -m review_sensei --help
/tmp/review-sensei-wheel-venv/bin/python -I -m unittest discover -s "$suite/dist_safe" -v
/tmp/review-sensei-wheel-venv/bin/python -I -m unittest discover -s "$suite/downstream" -v
/tmp/review-sensei-wheel-venv/bin/python -m pip check
```

The supported command, archive SHA-256 identification, required sdist entries,
and checkout-only lane are recorded in
`tests/fixtures/distribution-contract.json`, which
`scripts/check_sdist_contract.py` enforces against the built sdist. Declaring a
new lane helper or fixture means editing that contract and `MANIFEST.in`; CI and
the checkout lane tests read the contract rather than repeating the allowlist.

Do not make the clean-wheel environment import the checkout. The import guard is
armed by default rather than by environment variable: `review_sensei.__file__`
must be beneath `sys.prefix`, and beneath `$REVIEWSENSEI_CHECKOUT_ROOT/src` is
additionally rejected when that variable is set. Only
`REVIEWSENSEI_ALLOW_CHECKOUT_IMPORT=1` disarms it. All packaged schemas must stay
readable through `importlib.resources`.

Broad checkout tests remain `python -m unittest discover -s tests -v`, which
skips `tests/dist_safe` and `tests/downstream` because they are not packages;
`tests/conftest.py` makes `pytest` skip them too, so the packaged lanes only run
the documented way, from an unpacked sdist against an installed wheel.

## Deterministic evaluation changes

Run the corpus validator and fixture evaluation locally before changing corpus,
schema, or evaluator behavior. CI intentionally runs fixture mode only and
must not gain live-model, data-egress, or provider-secret flags. Corpus assets
must remain synthetic or explicitly licensed, fully inventoried, bounded, and
free of private data. Changes to thresholds, matching semantics, provider/model
configuration, or corpus cases require maintainer review and documentation
rationale. The live walkthrough in [`docs/evaluation.md`](docs/evaluation.md) is
opt-in and never belongs in CI.

## Licensing of contributions

Contributions are submitted under the license that already applies to the files
you touch (MIT for the project Software unless a file or directory states
otherwise, such as the CC0-1.0 synthetic corpus under `evaluation/v1/`). You or
your rightsholder retain copyright ownership of your contribution unless a
separate written agreement provides otherwise. The root
[`LICENSE`](LICENSE) copyright notice does not assign contributor copyright to
the maintainer.

This project uses the Developer Certificate of Origin (DCO) only. A DCO
certifies that you have the right to submit the contribution under the
applicable license; it is **not** copyright assignment. **Decision (issue #89):
DCO retained; no CLA is introduced by that work.** MIT already permits
commercial reuse and sublicensing subject to its notice conditions, so a CLA is
not a prerequisite for building or selling a product on top of this project.

Any future CLA or assignment must have a documented need, reviewed terms,
maintainer approval, and prospective adoption only. It would not automatically
cover past contributions or authorize removal of existing MIT (or other)
notices. See [`docs/ownership-and-licensing.md`](docs/ownership-and-licensing.md)
and [`TRADEMARKS.md`](TRADEMARKS.md) for ownership, brand, and commercial-boundary
documentation.

## Commits and pull requests

Use focused commits with clear imperative messages. Every commit must include a
`Signed-off-by: Full Name <email@example.com>` trailer. Use `git commit -s` and
ensure the email is one you are authorized to use. See the
[DCO](https://developercertificate.org/) for the certification terms.

Open a pull request from a branch and complete the repository pull-request
template. Link the issue when applicable, explain the behavior and data-handling
impact, list validation commands and results, and update user-facing docs,
examples, schemas, or changelog entries when they change. Do not include
credentials, private diffs/source, personal data, generated review output, or
raw provider responses in commits, fixtures, logs, screenshots, or review
comments. Reviewers may request an ADR, tests, or narrower scope before
approval.

Third-party Actions in `.github/workflows` and `examples/github-actions` are
pinned to full commit SHAs with same-line release comments. The sole exception
is the public ReviewSensei reusable workflow, which intentionally follows the
protected `@v5` setup channel and is checked by the OIDC broker at runtime.
The broker still accepts `@v4` during migration.
Dependabot updates the pins weekly; review the resulting diff and rerun
`python scripts/check_action_pins.py`. CI and tests must remain credential-free.

## Architecture and ADR policy

For changes to provider contracts, data handling, authentication, persistence,
deployment, public schemas, dependency direction, or other long-lived
architecture, update [`docs/architecture.md`](docs/architecture.md) and add or
amend an ADR under [`docs/adr/`](docs/adr/). Follow the process in
[`docs/process/adr-process.md`](docs/process/adr-process.md), including the linked
issue/PR, validation, consequences, and rollback. Documentation-only and
isolated test changes usually do not need an ADR; record a short no-ADR reason
in the PR when the boundary is ambiguous.

## Issues and triage

Use the [issue templates](.github/ISSUE_TEMPLATE/) for bugs, feature requests,
and documentation gaps. Include a minimal reproducible example and sanitized
environment details. Maintainers triage new issues, apply labels, request
clarification, identify duplicates, and record scope or dependency decisions.
The proposed labels and optional Discussions workflow are described in
[`docs/community.md`](docs/community.md). GitHub labels, topics, and Discussions are
maintainer settings; a pull request does not apply them automatically.

## Support boundaries

Use [SUPPORT.md](SUPPORT.md) and a public GitHub Issue for questions,
troubleshooting, reproducible bugs, feature proposals, and documentation gaps.
Do not post secrets, private source, private diffs, personal data, or raw
provider responses. Report suspected vulnerabilities through [SECURITY.md](SECURITY.md)
instead of a public issue. Support is best-effort and limited to the open-source
engine, its documented CLI/workflow, and sanitized reproductions; maintainers do
not operate a hosted backend or inspect private repositories.

## Roadmap and releases

The roadmap is tracked through [GitHub Issues](https://github.com/malsabbagh/review-sensei/issues)
and the priorities recorded in the issue tracker, rather than an independent
promise of dates. See [`docs/releasing.md`](docs/releasing.md) for the release
runbook. Maintainers own version selection, changelog entries, tags, package
publication, release attestations, and incident/yank decisions. Contributors
must not push tags, publish packages, or invoke release workflows unless a
maintainer explicitly delegates that responsibility.

## Maintainer decision-making

Maintainers are responsible for project direction, security boundaries, release
readiness, and final acceptance. Decisions should be grounded in the documented
architecture, ADRs, reproducible evidence, and the project's open-source-first
scope. Maintainers may request changes, defer proposals, or close issues that
are duplicates, unsupported, unsafe, or outside scope, while explaining the
rationale when practical. Community feedback is welcome, but approval does not
transfer release, merge, or publication authority.
