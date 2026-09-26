# ADR 0056 - Host-fact placement and honest approval presentation

Status: Proposed
Date: 2026-09-26
GitHub Issue: #181 (slice D)
Owners/Reviewers: Maintainers

Status note: this record stays `Proposed` deliberately at merge time, matching
the ADR 0055 precedent. The ADR process requires explicit maintainer approval
to mark a record `Accepted` (`docs/process/adr-process.md`); approval is
tracked with the epic rollout on issue #181, and the index row carries the
matching `Proposed` status.

## Context

The published review had three coupled problems. Placement of a finding was
decided from its own label: a non-blocking finding could open an inline thread
on a branch whose rules require every conversation to be resolved before merge,
which turns optional feedback into a merge blocker that overrides the host's
own rule. Severity and enforcement were conflated in presentation: advisory
mode relabeled a serious, demonstrated defect as an optional improvement, which
is the opposite of the honest reporting advisory mode exists to provide.
Approval eligibility was implicit: the finalizer's operator switch was treated
as the whole approval decision, so a partial analysis, a pending human
assessment, or incomplete coverage could converge to `APPROVE` on the
maintainer's behalf.

Epic #181 slice D fixes all three as one presentation-and-placement change:
shared typed renderers, stable finding identifiers, a host-fact placement
planner, and formatting fixtures that pin the published text.

## Decision

Placement is planned from host facts read through the GitHub adapter, not from
a finding's label. Before planning body placement for non-enforced feedback,
the publisher reads whether the target branch requires conversation resolution
— branch rules (`pull_request.required_review_thread_resolution`) then classic
branch protection (`required_conversation_resolution.enabled`). A positive
requirement is conclusive; only a conclusive absence permits optional inline
threads, and an unreadable, unparseable, or unexpected response leaves the
state unknown, which fails closed to the body. The planner's rule order is:
advisory mode, missing publishable anchor, pending human assessment, blocking
feedback (inline), policy that does not open advisory threads, host facts that
do not permit optional threads, and otherwise inline. A finding that blocks
approval keeps its inline thread and its gate under every conversation-
resolution state, because it is enforced feedback.

The review body explains placement once, using the rule that decided it
(advisory mode, a conversation-resolution requirement, or the general body
fallback), rather than repeating a note per finding. File-level findings keep
the ADR 0055 behavior: they are folded into the summary body for every review
event, and location fallback never changes `review_status` or a coverage
outcome.

Presentation keeps enforcement and severity as separate concepts. Advisory mode
states that enforcement is disabled and that its findings do not affect the
review gate; a serious demonstrated defect keeps its defect wording, its
severity, and its section, and optional feedback keeps the explicit "This is
not required for this PR." statement. The summary claims an approval only after
an actual successful approval operation, and otherwise distinguishes no
required fixes found, approval withheld with a reason, and an incomplete
review.

Automatic approval requires an eligible artifact: a complete review status, no
finding awaiting a human assessment, and coverage that is either absent (a
legacy or third-party result, which keeps the compatible path) or fully
reviewed. The operator switch still governs gate maintenance, so enabling
automatic approval and disabling it differ in who writes the approval, not in
whether an open blocking root keeps `REQUEST_CHANGES` asserted. An ineligible
run reports `approval_withheld` instead of approving. Eligibility is evaluated
once, on the admitted artifact that is actually published — never on the
pre-admission input — so the published summary state and the approval
operation cannot disagree.

## Scope

`src/review_sensei/placement.py` is new and owns the placement rules and the
placement notes. `src/review_sensei/presentation.py` owns the typed renderers,
the stable identifiers, and the summary wording.
`ReviewPublisher.publish` wires both, reads the host facts, and passes
`approval_permitted` to the shared finalizer, whose signature gains that
keyword with a default that preserves existing callers.

Not in scope: the event selection rules (`REQUEST_CHANGES` for blocking
findings, `COMMENT` otherwise, ADR 0035), the exact-head and thread-resolution
safety rules (ADR 0030/0032), marker hashing and duplicate reconciliation
(ADR 0026), and any change to persisted schemas or stored state. Placement and
presentation are recomputed from the current review and host facts on every
publication.

## Consequences

Optional feedback can no longer become an unresolvable merge blocker on a
branch that requires conversation resolution, and a reader of the body learns
why feedback is not an inline thread. The cost is that the publisher performs
up to two additional bounded reads (branch rules, then branch protection) only
when a run would otherwise open an optional inline thread; a run whose findings
are all blocking, all advisory, all body-bound, or already consolidated by
policy performs no such read.

Advisory mode keeps reporting a serious defect as serious while stating that
enforcement is disabled, so advisory adoption does not silently downgrade the
severity of what it reports. The cost is that advisory summaries no longer use
the "required fix" wording, which was previously reused for defects the run
could not enforce.

Approval can now be withheld for a run whose operator enabled automatic
approval but whose analysis was incomplete, partial, pending a human
assessment, or published as `summary-only`. The cost is that a repository that
relied on a partial run eventually approving now sees `Approval withheld` with
the reason and, when it re-runs to completion, the approval.

## Alternatives considered

Deciding placement from the finding label alone (the previous behavior) is
rejected: a label is not host authority, and GitHub's conversation-resolution
rule would have been overridden by ReviewSensei's own classification.

Reading conversation resolution only from classic branch protection was
rejected: repositories that express the requirement as a ruleset would have
opened optional threads that must be resolved. Reading only branch rules was
rejected symmetrically. Both reads fail closed to the body when unreadable.

Keeping the previous advisory relabeling was rejected: it contradicts the epic
requirement that model severity and a finding's disposition stay distinct, and
it makes advisory output less honest than the operator modes for the same
finding.

Treating the operator switch as the whole approval decision was rejected: an
ineligible artifact converging to `APPROVE` attributes a maintainer decision to
a review that never completed.

Suppressing the finalizer entirely when the review is ineligible was rejected:
it would stop the gate from being re-asserted for blocking findings, so a
repository could sit in a stale approved state after a partial re-run.

## Validation

`tests/test_placement.py` covers the planner's rule order, each body reason,
the reason-aware note priority, and rejected inputs. `tests/test_presentation.py`
covers the typed renderers, advisory wording, escaping (including a hostile
link target staying inert and Unicode and multiline code rendering),
size bounds, and stable identifiers.

`tests/test_presentation_golden.py` publishes thirteen scenarios through the
real `ReviewPublisher` against the shared fake HTTP transport and compares the
published summary and inline comments, byte-for-byte, with
`tests/fixtures/presentation/review-bodies.json`: clean, required, optional,
advisory high-impact defect, uncertainty, cross-file/unanchored, partial
coverage, failed review, resource handoff, more than three optional items,
conversation-resolution required, retry deduplication, and the formatting edges
scenario. Invariants assert balanced code fences, no forbidden artifact in any
published payload, no visible token wider than a narrow-layout budget,
no unescaped non-http(s) link target, stable identifiers across reruns, and
that required findings never disappear from the summary.

`tests/test_github_publication.py` covers advisory, incomplete, partial, human
assessment, and coverage-driven approval suppression against the fake
transport, including that the published event stays `COMMENT` and the summary
keeps its reason.

Beyond the automated suite, the golden bodies were rendered at a phone-width
column and inspected directly: the formatting-edges body wraps without
overflow, the escaped hostile link and forged marker render as inert text, the
real documentation link renders as a link, and the advisory and consolidation
scenarios read as intended.

## Rollout and rollback

Rollout is a normal release of the package and the `v5` workflow channel; no
stored state is migrated and no operator configuration changes. Rollback is a
version rollback of the package and the workflow channel: the presentation
layer, the placement planner, and the approval-eligibility check all live in
code, the fixture and tests are repository-local, and the publisher's stored
markers and review bodies remain valid because the reconciliation marker format
and hashing are unchanged (ADR 0026).

## Follow-up work

The final approval step for this record is maintainer review of the slice D
pull request. Later epic slices consume these contracts: slice F adds the check
gate and the default approval behavior, and slice G records the transition and
the acceptance matrix that cites these fixtures.

## Links

- [Issue #181](https://github.com/malsabbagh/review-sensei/issues/181)
- [ADR 0026](0026-stable-reviewsensei-learning-pull-request-identity-and-reconciliation.md)
- [ADR 0029](0029-finding-classification-and-lens-presentation.md)
- [ADR 0030](0030-gate-app-approvals-on-resolved-review-threads-and-exact-head-review-safety.md)
- [ADR 0032](0032-blocking-finding-classification-for-approvals.md)
- [ADR 0035](0035-request-changes-for-blocking-findings.md)
- [ADR 0055](0055-merge-focused-default-and-legacy-retirement.md)
