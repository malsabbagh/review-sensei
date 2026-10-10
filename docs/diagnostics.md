# Diagnostics and plan previews

Complete selected feedback's isolated adapter raises `FeedbackAdmissionError`
with a closed diagnostic and no raw source/token/provider payload. Examples are
`feedback_source_oversized`, `feedback_total_oversized`,
`feedback_source_missing`, `feedback_source_changed`,
`feedback_source_unauthorized`, `feedback_source_association_invalid`,
`feedback_read_budget_exhausted`, `feedback_targets_invalid` and
`feedback_prompt_oversized`. These describe admission or fencing failure, not
resolution or approval. The integrated caller must preserve prior authority and
report the refused assessment separately from chat delivery. See
[human assessment workflows](human-assessment.md) for finite limits and the
current integration boundary; no CLI diagnostic schema is activated by this lane.

`review-sensei doctor` performs bounded offline checks for package metadata,
packaged stages/categories, configured trusted-base paths, provider mode, and
the same JSON loaders used by the review runner. Custom `--stages-dir` values
use the same catalog selection as review: the category catalog stays unset
unless `--categories-dir` is supplied. Stages that declare `category_ids`
therefore require `--categories-dir`. Malformed configuration, empty
directories, and symlinked assets are reported as `action` rather than
silently treated as available. Doctor also records that symbol-aware source
context is an opt-in trusted-base policy that stays disabled by default.
It reports the review-convergence policy resolved from `--review-mode` or
`REVIEWSENSEI_REVIEW_MODE` (`merge-focused` by default). Explicit historical
`legacy` configuration reports an actionable migration error before execution.
Supported modes report `enforcement=publication`
because the blocker-admission evaluator now sits in front of GitHub review
events. It never calls a model, mints a broker token, or writes to GitHub.
When `REVIEWSENSEI_REVIEW_SHADOW` is set to an operator mode, doctor reports
an observation-only `review-shadow` check; publication stays on the resolved
review mode. `legacy` is not a valid shadow target.

The default `merge-focused` policy requires trusted session state for round
admission. With no configured session ledger, `doctor` reports `status=action`
and exits 2, even when its package/provider checks pass. `plan` includes
`session-ledger-required` in `skip_reasons`. Its `status=ready` only says the
diff was analyzed; it is not admission to execute a review. Supplying valid,
identity-bound session state is an execution prerequisite, not a reason to
fall back to legacy. A default setup-v5 install never hits this case on the
hosted path: its generated workflow passes `--github-session-ledger`, so a
`status=action` result for a missing ledger means you are running the bare
local CLI without that flag, not that a hosted install is broken. A local
review supplies its own ledger only when you ask for one with `--local-session`
(platform per-user state directory, overridable with `--session-ledger`). These
diagnostic commands do not initialize or mutate a ledger, reset counters,
invoke a model, or write to GitHub.

Offline runs do not open sockets. Passing `--network` enables read-only probes
that distinguish:

- an unreachable local runner endpoint
- a missing installed model
- inaccessible repository metadata, only when `--repository` is supplied and a
  `GITHUB_TOKEN`/`GH_TOKEN` is already present
- unavailable compatibility evidence, when
  `--compatibility-manifest` or `REVIEWSENSEI_COMPATIBILITY_MANIFEST` is set

Loopback probes use GET `/api/version` and `/api/tags` only. They never POST a
generation request and never mint a broker capability. Non-loopback endpoints
stay `unknown` unless `--allow-data-egress` is also passed. Missing optional
inputs (no repository, no manifest) stay `unknown` and do not fail an otherwise
healthy probe.

`review-sensei plan --diff <path>` validates a supplied diff with the same
bounded `analyze_diff` path used by review. It prints the selected stages,
category ids, budgets, identity fields including `--base-sha`/`--head-sha` when
supplied, the resolved review-convergence policy, skip reasons, and a
zero-call/zero-write operation summary. Without a
diff, the plan is explicitly incomplete. Use `--json` for the versioned
machine-readable contract.

Exit codes:

| Command | Code | Meaning |
| --- | --- | --- |
| `doctor` | `0` | Configured checks passed (offline network is recorded but does not fail the run) |
| `doctor` | `2` | Action required, or a diagnostic validation error |
| `doctor` | `3` | An explicitly requested probe remains unverifiable with current permissions |
| `plan` | `0` | Plan is ready (diff analyzed) |
| `plan` | `2` | Input or validation error |
| `plan` | `3` | Plan is incomplete (no diff supplied) |
| `review` | `0` | The review completed and no required fixes remain |
| `review` | `1` | The review completed with required fixes remaining |
| `review` | `2` | The review could not complete, the input was invalid, or an operator must intervene |

`review` exits `1` and `2` print one `review-sensei: reason=<token>` line on
stderr; the selected `--format` never changes the exit. Exit `2` includes
`already_published`: a repeated `--local-session` review of an unchanged change
serves its persisted round instead of paying for a second inference call, so it
reports that reason and writes no new document. Hosts and launchers that
select a lane from the historical 0/1 contract pass `--exit-semantics
operational`: it exits `1` only for the failure statuses
(`provider_failed`, `budget_exhausted`, `publication_failed`,
`action_required`) and `0` otherwise, including a completed review with
required fixes.

## A valid session remains paused after a failed analysis

A held reservation can block admission even when `operator_paused` is false.
After the reviewed recovery runtime and workflow are installed, an authorized
maintainer can comment `@reviewsensei review continue` on that PR. The command uses
GitHub Actions reads to prove the originating run has completed, refuses an active
owner run, rechecks ledger ownership and the current PR head, and charges
one failed attempt to the abandoned head. Run a new review only after the
command reports successful recovery; continuation itself performs no inference.

Old records without OIDC owner metadata can recover only when exact-head PR-run
and failed analysis-job metadata uniquely identify the origin, and the record's
generation still matches the attached analysis transaction. Later writes can
move the timestamp, so those legacy records fail closed rather than guessing a
new origin. Missing, changed,
ambiguous or truncated evidence requires investigation. Unrelated CI does not
block recovery; only the exact owner proof and session fences authorize it. The App
installation must accept Actions read and the broker must support the separate
`review_actions` capability. Existing caller workflow permissions stay unchanged. Expiry,
integrity failures and saved publication work keep their existing recovery paths.

Local `review continue` cannot reclaim a held reservation: it reports
`held reservation requires authenticated Actions recovery` and preserves the
hold. A local ledger has no authenticated Actions completion proof. Hosted
recovery applies only to its corresponding hosted session ledger; an abandoned
local process requires investigation rather than a silent unpause or timer unlock.
