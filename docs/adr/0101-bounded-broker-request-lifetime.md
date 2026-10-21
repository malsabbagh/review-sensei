# ADR 0101: Bound broker execution and preserve safe failure evidence

- Status: Proposed
- Date: 2026-10-21
- Decision owner / reviewers: ReviewSensei maintainers; independent Astra review
- Linked GitHub Issue: not configured; maintainer-authorized PR #340 repair
- Linked PR: [#341](https://github.com/malsabbagh/review-sensei/pull/341)

## Context

An initial hosted reply capability exchange attaches caller read accounting but
has no physical-dispatch control envelope. On that legacy route, a stalled public
Git or JWKS fetch/body read has no application deadline. The pending isolate-wide
JWKS promise can also block other callers. Client failures collapse opening,
reading, decoding and size failures into one message. Retained incidents do not
establish their historical network cause or whether an uncertain POST completed.

## Decision and scope

Bound each broker exchange or session-grant verification to 25 seconds, and each
upstream fetch plus body read to 10 seconds. These are new conservative service
ceilings, not historical measurements: the ordinary client retains its existing
30-second timeout and has a five-second response margin. The optional client
`X-ReviewSensei-Deadline-Unix-Ms` header is computed from its already admitted
`min(timeout, remaining)` allowance. A validated header can only shorten the
service ceiling. It carries no operation identity, authority or dispatch funding;
malformed or expired values refuse before upstream work. An existing controlled
request retains its smaller original deadline, exact dispatch liability and
receipt, redirect refusal and no-refund semantics. No cap or retry count rises.
Admission and preparation latency consume that same client window: immediately
before dispatch, shorten the transport timeout or refuse without another debit.

The operation owns fetch abort signals and body-reader cancellation. Deadline
checks fence every later dispatch, returned credential and cache commit, including
transports which deliver a response after cancellation. A transport failure still
leaves a token POST outcome unknown; this change never automatically repeats it.

Keep the five-minute completed public JWKS cache, but do not share pending I/O
across requests. Each miss owns and pays for its own bounded fetch. A caller
cannot cancel another caller, and an older/expired fetch cannot overwrite a newer
cache attempt. Signature, issuer, audience, token-age and claim checks remain.

Preserve the closed version-1 diagnostic envelope. A separate optional `lookup`
object carries fixed phase/failure enums and validated bounded GitHub request-ID
and numeric rate-limit/retry metadata. Legacy readers ignore that top-level
addition. Never include arbitrary response text, headers, exception text, URLs,
query strings or credentials. A generic HTTP403 remains unclassified unless
validated rate metadata actually supports a narrower interpretation. The client
reports fixed open/read/decode/size failure classes, elapsed monotonic time,
configured timeout and validated response status/correlation when available.

## Alternatives and consequences

A shared owner fetch plus independently cancelled waiters was considered. It
complicates physical funding, caller deadlines and Cloudflare request-I/O ownership.
Completed-only caching instead makes simultaneous cold misses issue separate
bounded reads; each actual fetch consumes its own original dispatch. No reusable
caller ABI, App permission, secret, trust policy, feature opt-in or version tag
changes. Source alone does not change the deployed Worker or installed client.
The shared GitHub transport also bounds standalone setup reads to ten seconds per
request; those callers do not acquire an aggregate exchange allowance. Broker
entrypoints explicitly supply their one shared whole-exchange lifetime.

## Validation

Public GitHub adapter header/body stall regressions fail on the base and pass with
cancellation. Deterministic tests cover whole-operation and shorter caller/control
deadlines, no late mint dispatch, independent JWKS cancellation/TTL/late completion,
ref/tag-object HTTP403 metadata, malformed bodies and redaction. Client regressions
distinguish timeout/transport/JSON/UTF8/size failures and assert one POST only.
Existing authorization, provenance, response-size, accounting and identity tests
remain required. Repository source, packaging and Worker gates are separate from
any later actual deployed qualification.

## Rollout, rollback and follow-up

Normal review/main CI precede a new paired Python release and a compatible Worker
update. Qualify new immutable artifacts, deploy exact reviewed Worker bytes without
settings/secret changes, and promote the exact signed channel only through existing
gates. Never rewrite 0.8.7/0.3.7 tags or relabel historical receipts. Legacy clients
remain bounded by the server ceiling; new clients supply the shorter deadline and
understand optional lookup facts. Roll back by a reviewed forward release and
explicit compatible Worker rollback; do not alter permission or trust settings.

Before resuming PR #340, reconcile the original uncertain POST outcome and current
review state, then require separate action-time authorization where applicable.
Historical trace access remains an attribution limitation; fresh authoritative
state can independently establish whether a new attempt is safe. The npm publisher
migration is a separate activation hold. This repair does not prove a historical
timeout or by itself clear a review retry.
