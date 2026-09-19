# Diagnostics and plan previews

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
`REVIEWSENSEI_REVIEW_MODE` (`legacy` by default). `legacy` is display-only and
does not change publication. Operator modes report `enforcement=publication`
because the blocker-admission evaluator now sits in front of GitHub review
events. It never calls a model, mints a broker token, or writes to GitHub.
When `REVIEWSENSEI_REVIEW_SHADOW` is set to an operator mode, doctor reports
an observation-only `review-shadow` check; publication stays on the resolved
review mode. `legacy` is not a valid shadow target.

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
