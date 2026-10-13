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

The output directory must not already exist. Exit 0 requires all five suites to
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
