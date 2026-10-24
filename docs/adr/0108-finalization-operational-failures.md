# ADR 0108: Observe operational finalization failures

Status: Proposed. Implemented and published in hosted 0.3.14; not available
in hosted 0.3.13. Publication is separate from channel activation.

The published diagnostic observes `verify` and finding-disposition
re-finalization. It does not establish the cause of historical live failures or
prove automatic cleanup or native approval succeeds.

An exact-head complete review with all current human decisions honored can have
a successful REST check while later automatic thread cleanup and native approval
remain unconfirmed. Existing catches deliberately preserve committed decisions
and uncertain write outcomes, but discarded the failed operation stage. This
prevented distinguishing a rejected GraphQL read, original-call exhaustion,
changed authority, or an uncertain approval response. Owner-read probes cannot
reconstruct historical App permissions or shared request accounting.

Retain a request-local, closed operational observation alongside the existing
publication result. The first failure in each fixed phase and bounded aggregate
counts survive later failures. Record stage, an allowlisted failure category,
bounded HTTP status/GraphQL type, and existing budget calls. Unknown causes stay
unknown. Never retain exception messages, remote response bodies, credentials,
source, actor data or arbitrary error keys.

The existing budgeted transport observes its existing invocations and responses
without adding requests, consuming calls or refreshing its envelope. Its
identity and charging rules remain unchanged. Report 64 total calls separately
from the ordinary limit of 60 with four reserved. A request attempted through an
HTTP adapter is not proof of network dispatch; that distinction is explicit.
Pre-approval read failures and lost approval responses therefore remain distinct
without weakening uncertain-outcome reconciliation or at-most-one approval.

Emit a separate `approval-operation-failure` schema 1.0 JSON line when failures
exist. Preserve the existing `approval-progress` schema and terminal position,
all result flags, eligibility, current-source fences, retry behavior and durable
records. Observations never grant authority or feed check/approval decisions.
The implementation is hosted-adapter scope; it does not require an engine, npm,
Worker, credential, repository-policy or permission change.

Validate through real command-finalization HTTP seams: a successful check plus
rejected GraphQL reads, exhausted original calls before approval, first-cause
preservation, lost committed approval responses, stale/edited authority refusal,
and secret-shaped error redaction. Preserve production-broker regression lanes;
synthetic injected causes do not establish the historical live failure cause.

Rollback removes the optional observation only. Published review evidence,
snapshots, resource limits, signed releases and approval authority are unchanged.
