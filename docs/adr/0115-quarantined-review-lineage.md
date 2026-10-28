# ADR 0115: Isolated lineage for an unresolved review predecessor

- Status: Proposed; local implementation and qualification
- Date: 2026-10-28
- Decision owner/reviewers: repository owner; Astra High
- Related issue: user-directed recovery of PR #387; downstream verification #380
- Pull request: pending correction qualification

## Problem

A stopped review can retain a complete result while its publication and physical
request effects remain uncertain. Fixing the original source defect does not
complete that run. Ordinary terminal accounting rollover must continue to refuse
it. Reusing its mutable ledger comment would allow a previously dispatched PATCH
to overwrite a new run's accounting and obligations.

## Decision

Provide one explicit, maintainer-authorized quarantine transition for a bounded,
source-recognized pre-publication failure. Require a changed current head and a
positive, finite target-isolation classification, together with authenticated
cessation of the original owner. Cessation is not settlement. An operation whose
old effects could reach the new ledger, current-head check or current authority
is ineligible.

The old root is never written, deleted, completed or renamed. Retain its exact
authenticated snapshot, accounting/configuration, transaction, original result,
session counters, decisions, reservations, history and uncertainty. Treat its
accounting as the last authenticated snapshot, not final measured usage. Recover
none of its unused capacity or uncertain liability.

Create a separately identified ledger. Its first confirmed write contains the
immutable predecessor relationship, preserved session state, complete historical
assessment hold, exact authorized transition identity and accounting for a
genuinely new run under current policy. Serialized historical evidence is not a
write grant. Current head/base, policy, authority and maintainer permission are
independently revalidated before effects.

Discovery follows only authenticated lineage. It does not choose the largest
generation, newest timestamp or highest comment ID. Reject duplicate roots,
forked successors, foreign producers, dropped or corrupt references, unsupported
versions and incomplete discovery. A versioned terminal marker must cause old
readers to refuse, including when a delayed old PATCH changes the predecessor's
body. A successor never falls back to its predecessor after activation.

## Creation and interruption

All cooperating review, reply, command and quarantine writers use the existing
serialized PR lane. Comment POST/PATCH is not compare-and-swap. Exact preparation,
pre-effect fences and authenticated readback detect conflicts; they do not claim
a distributed lock against privileged external tampering.

One authorized creation attempt may issue one new-root POST. A stored lost
acknowledgment can progress only after complete exact owned readback. An absent,
conflicting or incomplete readback cannot authorize another POST, inference or
publication. A later process must not infer creation non-entry merely from an
absent new root. The durable, cross-run one-shot intent boundary must be qualified
before this transition is executable.

That boundary is a positive registered Actions execution slot. The installed,
reviewed append-only registry binds one predecessor scope to one dedicated
dispatch-only numeric workflow ID, its exact source blob, run number 1 and
attempt 1. A recreated workflow, rerun or later dispatch refuses creation even
when the first run or new root was deleted or omitted from a listing. Run-name
and bounded census evidence may detect conflicts, but absence never creates a
slot. Registration is empty until the disabled workflow's identity has been
obtained read-only and registered through a separately reviewed activation.
No automatic replacement slot is available if the first execution fails before
POST. Existing predecessor registrations cannot be deleted or reassigned.

Before activation, failure leaves the predecessor untouched. After activation,
the new ledger and its hold remain authoritative and blocked. No crash, retry or
rollback resets session limits, restores abandoned allowance or reconstructs
process authority from a diagnostic file.

## Historical assessments

Extract only the closed hold, monotonic retention, authenticated reader,
eligibility enforcement and individual discharge contracts from the reviewed
historical-assessment design. Adapt their origin to quarantined retained evidence;
do not require a nonexistent historical review ID or completion receipt.

Reconstruct assessments from authenticated original context. Raw result flags
alone cannot establish an empty obligation set. Preserve the complete retained
result and join unresolved earlier published assessments. The hold must exist
before fresh inference and must survive empty fresh output, rescans, commands,
replies, verify and all approval finalizers.

Discharge requires a current publication exposing the exact carried inventory,
authenticated original evidence, a current authorized individual decision and a
full instance/scope identity. Prefixes, foreign sources, stale authority and
collisions with fresh findings refuse. A fresh recurrence is independent. Neither
quarantine nor a new clean review claims the old run completed.

## First full analysis and retained evidence

The first successor analysis validates the complete inherited baseline but uses a
full pass for the new head. A versioned reservation hash binds that choice to the
immutable lineage and the independently derived ordinary key, policy,
configuration and evidence digests. Recovery reconstructs those inputs before
recognizing the same reservation; it cannot infer another pass from missing
output. Once a fresh baseline is committed, ordinary convergence resumes. Partial
coverage remains partial, and all historical holds remain independent.

The predecessor snapshot uses the existing bounded lossless `zlib-json-v1`
envelope with explicit representation, independent encoded/decoded caps and a
full `SessionRecord` decode. Registered original-record SHA equality is mandatory.
Compression changes no fields, counters, reservations or accounting. Original
selected context retains ordered occurrences, full source inventory and selected
content digests; each consuming process reconstructs the exact Git-bound text.

Preparation reuses immutable bytes from one authenticated complete comment
listing. Only the pure original-source read window coalesces current-scope reads
between fresh entry and exit checks. Every carrier and root effect independently
rechecks current maintainer identity, default-branch policy and head/base. Missing
carriers cannot fall back to stale partial discovery.

## Census, checkpoints and later startup

Quarantine discovery uses one closed read-only GraphQL query with complete comment
bodies, numeric Bot identities and exact repository/PR associations. Only that
constant query and its closed variables classify a POST as a read. Each census
requires a stable complete unique inventory of at most 1,000 comments, unique
opaque cursors, no GraphQL errors or partial result, at most ten physical attempts
and the existing 512-KiB response bound. Prior ordered body-size hints select page
sizes only; every actual byte is read and authenticated again. Oversized responses
may expose a closed object count, never partial comment content or authority.

An ordinary quarantine checkpoint takes one fresh complete census, two exact owned
root GET fences, and an authenticated current-scope check immediately before its
single PATCH. It preserves all original history, queue and individual-decision
guards. Its process-issued snapshot binds the actual caller, ledger, operation,
root, digest, generation and lineage, then expires. A lost acknowledgment can use
only its originally protected readback tail; it cannot replenish or replay the
write. The existing own-release cleanup exemption additionally requires identical
canonical lineage, so it cannot exempt a historical-assessment change.

Later ordinary startup on an authenticated quarantine successor follows the same
fresh-census and two-root-fence boundary. Explicit current-scope checks precede
archive preparation and PATCH. The original startup issuer still requires a ceased
terminal previous owner, immutable archive readback and the new run's finite
accounting; its conservative 34-call retention preflight remains unchanged. The
startup snapshot is bound to the actual first-attempt startup caller, positive
slot and reservation. It permits one PATCH and at most two exact GET attempts
within the original tail. Missing or conflicting confirmation refuses activation.
Neither path writes the old predecessor root or claims native compare-and-swap.

## Budgets and compatibility

Charge every preparation, source/cessation/intent read, immutable carrier write,
new-root creation and confirmation to the genuinely new run's normal finite
current-policy allowance. No old allowance transfers. Preserve saturated session
review limits. Complete inventory and mandatory carriers must fit their existing
byte, item, pagination and future-growth bounds before activation.

Keep ordinary accounting binding and terminal rollover unchanged. No Worker
permission expansion or deployment is intended: prove actual current authority
and `known`/`enrolled` compatibility through production Worker contracts. An
unsupported combination refuses rather than falling back to an old ledger.

## Acceptance

Public-entry synthetic transports must exercise delayed old-root PATCH and
old-head check POST/PATCH after new activation, stored/unstored lost creation
acknowledgment, crashes, repeated/racing transitions, duplicate/forked roots,
permission revocation, head/base/policy changes, corrupt origin/inventory/counters,
empty fresh reviews, exact individual decisions and fresh-finding collisions.

Measure a complete quarantine → fresh review → disposition → verify journey,
including physical caller requests, retained bytes and remaining limits. It must
yield one legitimate current approval while the predecessor remains unresolved
and all history and charges are retained. Reader/version and Worker combinations,
schemas, workflow recognition, installed wheels and distribution declarations
are required gates. Raw incident payloads remain outside source and fixtures.

## Scope and consequences

This correction covers the recognized pre-publication failure and its authenticated
successor consumers. It does not settle uncertain predecessor requests or add a
general replay route. Exact reviewed base B remains distinct from current policy
commit P. A consumed creation slot can leave recovery blocked even before POST;
absence or deleted executions never allocate a replacement. Fresh complete reads
and retained carriers cost capacity, and independent discovery or evidence bounds
may refuse while the caller allowance still has unused requests.

## Alternatives

Resuming the old run still requires unavailable settlement and original-context
continuity. Adding a flag to its mutable root is vulnerable to delayed PATCHes.
Deleting state or creating a replacement pull request discards the preservation
boundary. A new isolated lineage avoids those effects without inventing old-run
completion, at the cost of explicit discovery and obligation-carry support.

## Rollout

Implement and qualify a separate correction from main. Obtain normal independent
review, exact-head gates and a separately authorized publication. The sole
publisher owns paired release and qualified channel promotion. Live quarantine
and fresh review need their own exact-scope authorization; neither follows from
this local implementation. Rollback pauses new work and uses a compatible reader
or reviewed forward fix, never the old ledger as fallback.

## Follow-up work

Qualify the immutable correction and installed packages, obtain independent review
and complete required platform CI before merge. The sole publisher owns necessary
version and channel promotion. Obtain the disabled workflow's exact numeric identity
and source read-only, then review the separately scoped registration and activation.
Reconcile the live predecessor, current B/H/P, native original-review identity and
authenticated cessation before the supported one-shot transition. Preserve all
charges and individual held obligations through subsequent review and verification.
