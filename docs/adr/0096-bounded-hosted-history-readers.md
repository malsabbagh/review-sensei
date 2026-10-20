# ADR 0096: Recover oversized hosted histories within existing read envelopes

- Status: Proposed
- Date: 2026-10-20
- Decision owner / reviewers: ReviewSensei maintainers
- Linked GitHub Issues: #238, #240, #246, #267, #269
- Trigger: PR #316 installed hosted 0.3.4 startup and cleanup refusal after the broker accepted its session

## Context

The broker and inline publication reader can recover oversized GitHub list pages,
but the installed hosted startup, operation admission, ordinary review lists and
review-thread queries still have fixed-page readers. PR #316's 27 issue comments
serialize to 620,839 bytes, above the unchanged 512 KiB response cap. Successful
broker admission therefore does not establish that the installed client can load
the ledger or finish publication. A sealed activation fence also needs a viable
page size before its exact prepaid request URLs are frozen.

## Decision

Ordinary REST list enumeration retries the same exact item offset with smaller
pages. Every failed physical read counts against the original ten-request walk;
the 1,000-item and 512 KiB per-response bounds stay unchanged. Explicit adaptive
callers retain their own existing envelopes. Operation admission uses this reader;
operation replay adds an aligned 50-item candidate within its existing ten reads.
Conversation and finalizer scans share the same REST history policy, including a
ten-item candidate. This avoids pinning repeated large-body scans at five items
and permits the combined captured comment/review history to finish within the
original composed budget.
Review-thread GraphQL queries retry the same opaque cursor with smaller `first`
values within one ten-request walk. Only typed response-size failures trigger
fallback. Status, shape, author, snapshot, cursor and completeness checks remain
required. Discovery completes before thread mutations.

A ledger selects a known smaller fence page size during ordinary discovery,
before sealing the activation plan. Both planned URLs and final-page validation
use that size. No fallback, replacement request, budget refill or changed URL is
allowed after sealing. New growth beyond a prepaid fence refuses activation.

No schema, stored evidence, approval rule, provider policy or wire protocol changes.
GitHub comments still do not provide distributed compare-and-swap. The separate
opt-in human-file library and run/job envelope readers retain their documented
bounds; this change does not activate or qualify those separate hosted flows.

## Qualification and rollout

See [the acceptance matrix](../validation/hosted-history-readers.md). Shared tests
run from checkout, extracted sdist against installed wheels, and the published
CLI qualification inventory. Synthetic fixtures reproduce the exact captured
record sizes without retaining review text or credentials. Failure preserves
pending obligations and retained publication recovery; no partial list escapes.

The executing workflow needs a newly published hosted wheel containing this fix.
Source and synthetic success alone do not qualify production: paired immutable
Python publication, signed major-channel admission and an actual bounded PR #316
retry are separate rollout gates. Revert source through a reviewed forward release
if necessary; never rewrite existing version tags or ledger history.
