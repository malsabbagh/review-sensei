# ADR 0139 - Finite grouped publication declarations

Status: Proposed
Date: 2026-11-14
Owners/Reviewers: Maintainers
GitHub Issue: User-directed bounded publication repair; no new issue
PR: [#462](https://github.com/malsabbagh/review-sensei/pull/462)

## Context

A grouped publication may need to retain a fixed set of future obligations
before the exact final review body exists. The existing staging-v2 attempt
flags mean that an individual attempt was durably claimed. Reusing those flags
for future declarations would change crash and cold-recovery semantics.

## Decision

Introduce an explicit staging-v3 preservation format for one grouped part,
its manifest, and its final review. Three immutable tokens identify logical
obligations, not dispatched requests, physical accounting debits, or receipts.
The final request digest appears only with positive part and manifest receipts.
A held state retains unconfirmed obligations and cannot be reopened.

Session records normalize and integrity-cover this version, preserve it across
unrelated changes, and reject loss, downgrade, changed context or reordered
receipts. Existing staging-v2 semantics and encodings remain unchanged. The
same staging component and total record byte limits apply.

## Scope

This decision adds parser, schema and monotone preservation support only.
Existing native publication writers still reject staging-v3. No producer,
request replay, recovery settlement, source lease or execution grant is enabled.
Logical declarations and physical unknown request charges remain separate.

## Consequences

New readers can preserve explicit future obligations without fabricating an
attempt or successful publication. Old readers refuse the new version rather
than guessing v2 semantics. A completed declaration is receipt data, not
independent evidence that a run settled or approval became permissible.

## Alternatives considered

- Reuse v2 flags: rejected because an undeclared native attempt and a future
  logical obligation have different recovery meaning.
- Discard unused tokens: rejected because absence is not settlement evidence.
- Increase storage or request limits: not required or authorized by this format.

## Validation

Focused controls cover v2 compatibility, v3 round trips and public schema,
transaction binding, immutable declarations, downgrade/discard refusal, and
existing writer refusal. Whole-route source, loss and funding qualification is
separate and remains required before any producer may issue this format.

## Rollout and rollback

Parser support precedes writers. No migration or live writer is introduced.
A future writer rollout must retain a reader that understands already-persisted
v3 records; reverting to a v2-only reader must fail closed, not erase records.

## Follow-up work

Prove original joint writer composition, accepted admission provenance, complete
fresh census membership, every response-loss branch, and protected local/total
funding before introducing any source-issued execution phase.
