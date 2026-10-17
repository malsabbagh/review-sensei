# Preserved PR 308 ledger regression

`pr308-session-ledger.json` preserves the exact comment body returned by GitHub
for [comment 6236936357](https://github.com/malsabbagh/review-sensei/pull/308#issuecomment-6236936357),
plus its public identity and timestamps. It contains no token or signing key.
`pr308-session-ledger-evidence.json` records hashes, repository/run identity,
versions and the limitations below. Tests never modify these fixture files;
continuation uses a temporary file-backed fake GitHub transport.

The snapshot's generation is 5, with five historical dispositions, one completed
initial review, zero completed verification rounds and zero failed attempts. Its
transaction is a successful publication on `67bbd8e`, not the later PR head.
The canonical record digest is
`cc8d4d1d76f4df4838b596e13b902545b35b004c3c9117830b480ca6357d88c2`;
the exact comment body digest is
`55150616bd9ee1ca726cd46381531147082af542485622b8f69ba07a841913bb`.

## What failed

Hosted run `39076608917` used released engine `0.7.1` and integration `0.2.1`
from workflow source `7c85643af40ed9baf2ea8a1e3c2a43a6fded759b`. Its public
ledger reader rejects this unchanged input: the released session schema's
`/dispositions` rule is `maxItems: 4`, but the document has five entries. PR 308
at `3fe7cfba58a50285b66740d91107718a03c26311` already aligns that schema cap
with the runtime cap and checks a candidate before writing it. Its public reader
accepts the exact snapshot without migration, rehashing or removing decisions.
This regression proves that existing repair against the preserved input.

The old-head dispositions are preserved as history and do not authorize approval
of the new head. A supported new reservation retires the completed historical
transaction; reads and pause commands preserve it. Duplicate commands preserve
record/counter state, although a new one-attempt mutation grant can itself be
recorded as consumed on a semantic no-op. Ambiguous writes require readback and
fresh authority, not replay of a consumed grant. Hosted generation checks remain
best effort; deployments must serialize writers as documented in ADR 0047.

## Authentication boundary

The ledger has a canonical integrity digest, not a cryptographic signature.
GitHub supplied the numeric Bot identity, author type, comment identity and
repository association through an authenticated, read-only API call. The
comment's `updated_at` predates the failure and a second fetch returned identical
body and update time. No artifact archives the response at the instant of the
failure. The original opaque broker grant and verification response are not
available; an original grant signature cannot be replayed or authenticated here.

Positive continuation uses an explicitly synthetic consuming verifier. This does
**not** establish the original broker grant's authenticity. Negative cases also
exercise the real `BrokerClient` with missing, incomplete and mismatched response
evidence. No broker request or ledger write is sent to a real service.

## Focused reproduction

From the checkout with development dependencies installed:

```sh
PYTHONPATH=src:packages/python/review-sensei-github/src python -m unittest discover -s tests -p test_pr308_ledger_regression.py -v
PYTHONPATH=src:packages/python/review-sensei-github/src python -m unittest discover -s tests -p test_media_command_reporting.py -v
```

Install the exact released pair into a disposable virtual environment, then run
this one regression from outside the checkout (expected failure):

```sh
/path/to/released-venv/bin/python -I /absolute/checkout/tests/test_pr308_ledger_regression.py PreservedLedgerRegressionTests.test_public_reader_preserves_exact_five_decision_record -v
```

Install engine and hosted wheels built from the candidate commit in a different
virtual environment. Run both complete test modules with `python -I` and their
absolute paths from outside the checkout. Isolation prevents checkout imports
from hiding incompatibility. This is narrow reader/reporting qualification;
real Actions and broader CLI/release qualification are separate work.
