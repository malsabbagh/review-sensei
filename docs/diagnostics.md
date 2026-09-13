# Diagnostics and plan previews

`review-sensei doctor` performs bounded offline checks for package metadata,
packaged stages/categories, configured trusted-base paths, and provider mode.
It never calls a model, mints a broker token, or writes to GitHub. Optional
network checks are reported as `unknown` unless a separately authorized probe
is run.

`review-sensei plan --diff <path>` validates a supplied diff and prints the
selected stages, budgets, identity fields, skip reasons, and a zero-call/zero-
write operation summary. Without a diff, the plan is explicitly incomplete.
Use `--json` for the versioned machine-readable contract.
