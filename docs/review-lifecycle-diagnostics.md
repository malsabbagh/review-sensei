# Review lifecycle diagnostics

The hosted `review-run`, `restart-run`, `command-run` and `reply-run` entry
points emit one structured stderr observation when their lifecycle context
returns or raises. The prefix is `review-sensei: lifecycle ` and the JSON
`schema_version` is `review-lifecycle-outcome-v1`. It is available with
`ARTIFACTS=none`; optional artifact collection is independent.

An observation is at most 4096 UTF-8 bytes including its prefix and newline.
If rendering or the size bound prevents a complete observation, the reason is
`diagnostic_unavailable` or `diagnostic_size_limit`. A broken stderr sink cannot
change the original exit or exception. A hard process kill may leave no line.
Missing observations do not establish successful execution or non-entry.

`phase` identifies the lifecycle boundary and `step` distinguishes bootstrap
work such as `default_policy_ref`, `broker_actions`, `broker_session` and
`predecessor_admission`. These are observations of existing work. A failed
boundary is not proof that an attempted request reached its remote service.
Where the existing broker exception supplies a validated closed diagnostic,
`broker` preserves its code, stage, upstream status and correlation ID without
copying the response body or lookup details.

Keep the following fields separate when investigating an incident:

- `scope.requested` records bounded IDs and hashes from entry arguments.
  It does not authenticate them.
- `scope.observed` is the PR read during bootstrap, not a promise about the
  current PR at the end of execution. `scope.observed_policy` separately names
  the default policy ref and its observed commit. Its content digest is null
  until the existing authenticated file check succeeds.
- `scope.admitted` records the scope accepted by the existing bootstrap.
  `scope.frozen` records the transaction, configuration and diff hashes actually
  available after analysis. Missing effective merge-base evidence stays null.
- `predecessor` records the retained root, caller and transaction facts when
  existing admission reads them. `transaction_present: false` differs from a
  transaction at `publication_succeeded`. Its original tail reason remains
  `unavailable`: a later refusal cannot reconstruct the earlier failure.
- `publication.accounting_terminal` and the local `caller` view describe the
  existing completion outcome and recorded counters. They do not supply missing
  effect proofs. Recorded completed/pending counts are not a full census of
  historical external effects. Null accounting differs from zero work.

A published review with unresolved findings or partial coverage can withhold
approval. Publication, caller completion, accepted decisions, coverage, native
approval and merge are distinct. For example, a missing caller terminal binding
after publication still refuses ordinary rollover. This observation cannot
settle that binding. An output-file error can also occur after a confirmed
caller close; the line retains both facts.

Logs and ledger timestamps may belong to a different clock domain from Actions
or review metadata. Keep their original values and provenance in incident
evidence. Do not infer an offset, cessation, deadline renewal or an event order
without an authoritative clock contract. A later Actions response's nested PR
metadata also does not establish the original reviewed head.

The collector adds no network requests, ledger writes, retries or credentials.
It omits raw exception text, comments, findings, diffs, prompts, provider output,
tokens and URLs. `authority: false`, `recovery_classification: not_evaluated` and
the unavailable transition reason are deliberate: no execution or recovery
decision consumes these observations. Existing recovery, accounting, coverage
and approval checks continue to apply.

This change supplies diagnostics and offline incident regressions. Typed
prospective completion, authenticated fresh generations after base/head
transitions, policy-admission refresh and historical recovery require separate
qualified changes. It does not enable them.
