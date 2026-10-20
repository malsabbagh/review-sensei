# ADR 0099 - Complete publication before optional thread supersession

Status: Proposed
Date: 2026-10-20
GitHub Issue: recovery qualification for PR #316
GitHub PR: [#316](https://github.com/malsabbagh/review-sensei/pull/316)

## Context

The application and publisher share one original evidence allowance. Old-thread
supersession was best-effort, but it ran before approval finalization and the
session publication transition. A valid review could be created while optional
cleanup exhausted the allowance needed to record its durable success. The
already-published and terminal session paths then skipped the unfinished cleanup.

## Decision

On a ledger-backed application path, finish required check/approval handling and
record publication success before running optional old-thread supersession.
Direct publisher calls still perform cleanup, after their mandatory finalizer.
Only a confirmed publication can schedule cleanup; failed or stale publication
results grant no cleanup authority. Mandatory failures remain failures.
An exactly reconciled lost, rejected or transient review-create response schedules
the same cleanup as a directly acknowledged publication. Reconciliation must
prove the complete native root, index and inline explanations before using its
review identity; a legacy marker alone grants no thread mutation. Unconfirmed
ownership or prose keeps the old thread open and cannot duplicate the review.

An explicit same-head publication-only retry of the retained complete-inventory
result checks
the original result identity, complete finding index, body explanations and
exact inline prose before resuming cleanup. It creates no duplicate review or
result/inventory parts and charges no analysis round. Unresolved old threads are
the work queue; resolved threads are skipped. Every mutation still requires the
live owned root, confirmed current replacement instances, latest unchanged
eligibility and open same-repository, exact-head/base, non-App-authored PR fences.

Cleanup uses the remaining original allowance, including its original deadline.
Before each candidate it checks the minimum root/fence/mutation liability. Extra
pagination remains read-only and may exhaust that allowance; all remaining work
then stays unconfirmed. After the mutable fences succeed, the mutation plus one
possible readback must fit. No optional work spends the four reserved finalization
calls, resets a budget, or changes a provider/round limit. An ambiguous mutation
is reconciled once and is never blindly repeated within that attempt.

Publication completion and approval eligibility remain distinct. Current/carried
inline blockers, body blockers, human decisions, missing evidence and failed
ownership fences retain their existing effects. Old-only supersession never
clears those facts. A published result may report `thread_resolution=unconfirmed`
and require a later explicit same-head publication-only retry of that exact
retained result and base. Publication CLI stderr must surface that pending work
without replacing a required-fix/decision or check-failure diagnostic. This retry
is not inference, `verify`, or a new full review. A freshly authenticated original
`publication_succeeded` transaction may omit raw-result backup retention and its
redundant ledger reload on that retry. Admission inventory, accepted-operation,
configured Bot, current PR, exact native root/index/full prose and normal
check/approval fences still run. Immediately before the completion no-op, a
fresh ledger read must prove the identical original terminal transaction,
result/context and generation. It replaces only the identical publication
completion PATCH; existing admission-marker CAS behavior remains intact. A
competing writer or absent complete native root refuses cleanup; terminal
cleanup cannot recreate an unavailable review or raw backup parts. Refusing that
optional retry preserves the original terminal publication without a success or
failure completion write. Pending and failed publication retention semantics are
unchanged. No single-invocation completion is promised
for a backlog larger than the existing allowance. A repeated identical state is
not evidence of progress. Independent actual-history full-lifecycle qualification
remains required before any live attempt; partial cleanup is not authorization.
Issue #270 and provider/broker/deployment changes are outside this work.

## Validation and rollback

The shared publication-recovery acceptance class runs in checkout, installed
sdist/dist-safe and post-package lifecycle lanes. Cover durable success despite
optional exhaustion, finite retry progress without duplicate writes, strict
root/head/base/eligibility fences, uncertain mutations and retained blockers.
Revert this ordering and retry slice to roll back; no persisted document schema
or permissions change. Previously unresolved threads remain visible on GitHub.
