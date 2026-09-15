# Diagnostics and plan previews

`review-sensei doctor` performs bounded offline checks for package metadata,
packaged stages/categories, configured trusted-base paths, provider mode, and
the same JSON loaders used by the review runner. Malformed configuration,
empty directories, and symlinked assets are reported as `action` rather than
silently treated as available.
It never calls a model, mints a broker token, or writes to GitHub. Optional
network checks are reported as `unknown` unless a separately authorized probe
is run.

`review-sensei plan --diff <path>` validates a supplied diff with the same
bounded `analyze_diff` path used by review. It prints the selected stages,
budgets, identity fields, skip reasons, and a zero-call/zero-write operation
summary. Without a diff, the plan is explicitly incomplete. Use `--json` for
the versioned machine-readable contract.

Exit codes:

| Command | Code | Meaning |
| --- | --- | --- |
| `doctor` | `0` | All checks passed |
| `doctor` | `2` | Action required, or a diagnostic validation error |
| `doctor` | `3` | At least one check is unknown (offline network) |
| `plan` | `0` | Plan is ready (diff analyzed) |
| `plan` | `2` | Input or validation error |
| `plan` | `3` | Plan is incomplete (no diff supplied) |
