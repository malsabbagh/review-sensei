# Checkout controller phase

The checkout runner accepts a repeated `--controller-module MODULE` option for
reviewed, self-contained `load_tests` modules. CI opts in
`test_provider_common_flow`. This module currently returns 116 tests; the count is
discovered, not used as a limit. Installed-wheel execution remains a separate `-I`
contract and does not receive this option.

Discovery runs once. The runner retains the hook's original returned suite and
validates the full decomposition before executing tests. Admission requires plain,
unmodified, non-repeated suite ownership, stable fixture identity, and disjoint
class-object and effective-module scopes between controller and worker cases.
Worker identities must retain the standard reloadable TestCase identity behavior.
Unknown hooks, custom ancestors, changed suites and ambiguous/shared ownership use
original-root fallback. Structural checks do not prove arbitrary hook purity;
adding an opt-in requires reviewing its global-state and ordering dependencies.

The controller subtree executes first, including fixture cleanup. Its coverage
collector starts before discovery and is saved and stopped before worker launches.
The remaining cases use the existing four-worker allocation: partition the full
inventory with the configured timing fixture, then subtract controller IDs from
each original bucket. Bucket IDs remain stable; empty buckets are not launched.
Controller tests and isolated workers never execute concurrently. Workers use the
launch environment and working directory captured before the controller phase.

Reports distinguish `mode`, `fallback_reason`, requested/effective worker count,
`controller` and `workers`. Worker `worker` is the original scheduling bucket;
`execution_ordinal` is its dense launch/log index. Each phase records its assigned
inventory hash and exact accounting. Add controller counts to worker counts when
checking the complete discovered inventory. `complete` retains its existing meaning
of complete inventory/coverage evidence; assertion failures still produce a
nonzero final exit even when that evidence is complete.

A normal controller assertion failure does not waive remaining worker tests.
Missing/damaged controller or worker evidence fails the lane. Abrupt exits retain
the existing final-completion supervision. Controller and all worker coverage files
are required before combination; the coverage floor remains 80%.

Regression coverage uses real small subprocess suites for phase ordering, one hook
call, configured case objects, socket/environment cleanup, four-worker concurrency,
failures, sparse/empty buckets, fallback and both coverage branches. A separate
real-repository discovery test observes actual `main()` dispatch manifests with
simulated test execution; it verifies all timing fixtures and that removing a real
checkpoint class cost changes dispatch. It is an assignment test, not whole-suite
execution. Remote CI on the resulting head remains authoritative for the full suite.

The focused real 116-test controller validation passed locally in approximately two
seconds with its sentinel environment and socket function restored. This is not a
hosted timing forecast or proof of overall CI acceleration. Preserve historical
review receipts and human assessments; this scheduling change does not settle
review-executor recovery.
