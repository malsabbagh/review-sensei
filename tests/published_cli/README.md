# Published CLI qualification

This suite runs unattended against two **exact PyPI wheels**, the engine
`review-sensei` and the hosted runtime `review-sensei-github`, installed together
in a newly created virtual environment. The hosted wheel must pin exactly the
engine version under test. It never installs `latest`, falls back to a source
checkout, configures credentials, or makes paid model/GitHub calls. Expectations
are ordinary contracts: known released defects remain failures.

```sh
python scripts/qualify_published_cli.py --version 0.6.23 --github-version 0.1.1 \
  --output /tmp/qualification-unique
```

The workflows read the hosted version from the released commit's
`packages/python/review-sensei-github/pyproject.toml`, the same source the hosted
runtime workflow installs from. Releases before the hosted split (0.6.22 and
earlier) cannot be qualified by this harness.

The output directory must not already exist. By default, exit 0 requires all six suites to
complete and pass; failed installation, propagation, process timeouts, malformed
receipts, or failed assertions exit nonzero. `summary.json` binds requested and
installed versions, package origins, both wheel SHA-256s, dependency versions, retry
receipts, suite totals, and exclusions. Per-case results include process exits,
bounded call accounting, coverage checks, and sanitized stdout/stderr from
synthetic inputs. The Actions artifact includes these machine-readable receipts.

The installer tries the requested public PyPI wheel at most six times (45 seconds
per attempt, five 15-second pauses), then fails explicitly. Dependency installation
has a separate 180-second bound. Pip configuration, caches and provider credentials
are excluded. Suites each have a 300-second process ceiling and smaller individual
case ceilings. The full lane has a 25-minute Actions ceiling.

`qualify.py` drives the installed console process with deterministic fixture
responses. `transports.py` drives real loopback HTTP and POSIX SIGINT with fake
Ollama responses. `operations.py` reproduces missing metadata tokens and complete/paginated operation-record discovery through the installed hosted CLI's `publish` command, run in-process so the blocked sockets and mocked publication continuation apply (the engine's `github review` forwards to a hosted subprocess). `operation_state.py` exercises trusted operation creation, body/author readback, partial acceptance and replay through an in-memory HTTP opener. `seam_boundaries.py` supplements console tests with installed
model/budget constructor boundaries; those are not evidence of live inference.
Cases test numeric/type limits, canonical config precedence, diff admission,
chunking and cumulative calls, normalized output, retry/correction, persistence
replay, CLI help/version, and coverage safety. Nine very large packing cases are
available with `qualify.py --large-total-work`; they are excluded from this bounded
default lane. Native/npm packages, injected GitHub reply regression suites, real
provider quality and all combinatorial interactions need separate evidence.

The `Qualify published CLI` workflow runs after `Release` completes. Its gate reads
the **same run attempt's** PyPI job from the GitHub API, requires its success, and
reads the exact `workflow_run.head_sha` manifest as data to resolve the version.
It does not execute or check out release-event code and does not download upstream
artifacts. A failed/missing publication gate is an explicit failure and tests are
not reported as passing. A manually dispatched run requires an exact version.
See GitHub's [workflow jobs API](https://docs.github.com/rest/actions/workflow-jobs).

The existing release workflow, tags, PyPI/npm publishing, deployment and secrets
are unchanged. Permissions are contents-read and gate-only actions-read. The
standard ephemeral GitHub token is used only by the publication gate. The workflow
must be merged onto the default branch before a future `workflow_run` event can
launch it. Its Linux/macOS Python 3.11/3.14 lanes require execution in Actions before
those platforms can be claimed qualified. Windows is not covered by this workflow.

The original 0.6.20 release failed five console cases and POSIX cancellation in
independent Mac qualification. Those original assertions were independently green for
unpublished candidate commit `a1854ffd37bb72309b35ee7aa051e555af1536af` (1,208 independent checks, including operation discovery/state).
The related hosted missing-token and page-two operation-record defects are also included in the workflow expectations. This integration should land after that fix PR is reviewed; a published release
containing those fixes is needed for an all-green installed lane. It does not
weaken assertions for current 0.6.20 or publish a replacement.

`live_canary.py` is an optional, separate local probe. It accepts only the existing
`OLLAMA_API_KEY` and the approved DeepSeek 4.1 Flash / GLM 5.3 cloud identities, uses
small synthetic input and a two-call limit, and saves hashes/counts instead of raw
provider payloads. The Actions workflow never invokes it and adds no credentials.

`lifecycle.py` reuses the packaged approval lifecycle and local CLI tests against
the exact installed engine and adapter. It covers trusted classification through
persisted eligibility, mixed media/generated confirmations, clean-process exits,
and the preserved PR308 five-disposition ledger. Synthetic broker fixtures are
not authentication evidence for the original grant. Known failures in an older
PyPI release remain failures; candidate-wheel success does not qualify PyPI.
The lifecycle receipt counts rows (including failing subcases) rather than claiming
that those rows are an independent test-method count.

The immutable major-validator baseline deliberately selects its original five
contracts with repeated `--suite` options, preserving the exact 0.6.23/0.1.1 wheel
digests. It does not claim those old bytes implement newer lifecycle behavior.
Default post-publication qualification runs all six suites; receipts always list
selected and excluded suites so a subset cannot be mistaken for full coverage.

Lifecycle contracts load from the five declared files under `tests/dist_safe`,
so a same-named module elsewhere on the search path cannot shadow them. Receipts
include `tests_run` separately from row counts, a sanitized `load_error` for file
import failures, and an explicit failed `empty-suite` row if discovery is empty.
Neither condition counts as passing qualification.

Lifecycle qualification treats every skipped method or subtest as a failed row
with `kind=skipped`, even though unittest itself permits skips. Any such row
invalidates qualification and forces exit 1. `tests_run` counts parent methods;
passing subtests produce their parent's passing row, while failing subtests
produce individual failed rows. These counts intentionally differ.
The loader validates both declared resolved paths before executing either file,
rejecting a contract file or directory symlink that escapes its declared location.
The qualification ref supplies trusted test code; these path checks do not
attest the harness contents or make arbitrary test code safe to execute.

The lifecycle inventory additionally runs the shared fresh-process baseline
recovery, provider deadline and OpenRouter CLI configuration contracts. These
exercise isolated synthetic state/transport against the exact installed wheels;
the baseline and deadline children independently check their installed origins.
The OpenRouter sentinel matrix captures the actual strict Morph request and
proves selected-key isolation/missing-key refusal without live provider I/O.
These bounded additions do not qualify all hosted operation/control combinations
or promote any model. See [the remaining acceptance matrix](../../docs/validation/remaining-lifecycle.md).
