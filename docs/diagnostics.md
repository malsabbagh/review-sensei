# Diagnostics and plan previews

`review-sensei doctor` performs bounded offline checks for package metadata,
packaged stages/categories, configured trusted-base paths, provider mode, and
the same JSON loaders used by the review runner. Custom `--stages-dir` values
use the same catalog selection as review: the category catalog stays unset
unless `--categories-dir` is supplied. Stages that declare `category_ids`
therefore require `--categories-dir`. Malformed configuration, empty
directories, and symlinked assets are reported as `action` rather than
silently treated as available.
It never calls a model, mints a broker token, or writes to GitHub. Optional
network checks are reported as `unknown` and do not fail an otherwise healthy
offline run. Passing `--network` keeps the report `unknown` because doctor
never probes an endpoint.

`review-sensei plan --diff <path>` validates a supplied diff with the same
bounded `analyze_diff` path used by review. It prints the selected stages,
budgets, identity fields, skip reasons, and a zero-call/zero-write operation
summary. Without a diff, the plan is explicitly incomplete. Use `--json` for
the versioned machine-readable contract.

Exit codes:

| Command | Code | Meaning |
| --- | --- | --- |
| `doctor` | `0` | Configured checks passed (offline network is recorded but does not fail the run) |
| `doctor` | `2` | Action required, or a diagnostic validation error |
| `doctor` | `3` | An explicitly requested `--network` check (or another unknown) remains unprobed |
| `plan` | `0` | Plan is ready (diff analyzed) |
| `plan` | `2` | Input or validation error |
| `plan` | `3` | Plan is incomplete (no diff supplied) |
