# Mention handle rollout

`@reviewsensei` is the canonical command and conversation handle;
`@ReviewSensei` works too. `@sensei` remains supported with no deprecation.

A caller on a consumer repository's default branch runs before the reusable
workflow. Older callers filter `contains(..., '@sensei')` and their embedded
resolver recognizes only that alias. Updating `@v5` alone cannot make the new
handle work in those installations.

## Bounded consumer patch

After a separately authorized product/runtime and broker promotion, prepare one
workflow-only PR per consumer, based on its current default-branch workflow:

1. In both `issue_comment` and `pull_request_review_comment` event arms, replace
   `contains(github.event.comment.body, '@sensei')` with
   `(contains(github.event.comment.body, '@reviewsensei') || contains(github.event.comment.body, '@sensei'))`.
   Preserve parentheses, association checks, bot checks, permissions and inputs.
2. Expand the embedded rescan mention gate and command prefilter to the same
   grammar used by `examples/github-actions/review-sensei-review.yml`. A new
   handle must reach `review`, `command`, or `reply` as appropriate.
3. Preserve consumer runner selections, concurrency and operator edits. Use a
   generated setup upgrade only when the entire previous caller matches a known
   managed shape; the installer refuses edited callers instead of overwriting them.
4. Validate both handles with all commands, re-scans, top-level and inline replies,
   case/boundary nonmatches, bot/association rejection, disabled mentions,
   body/reason bounds and exact-head authorization. Confirm the deployed broker
   accepts the new command handle and the runtime has the parser update.
5. Until caller, runtime and broker are compatible, keep using `@sensei`. Do not
   claim production end-to-end compatibility from parser tests alone.

The product PR updates current Python/Worker installer templates, the repository
caller and the example caller. It freezes the prior invocation-only v5 caller
for byte-exact migration recognition. Historical v3/v4/v5 fixtures and templates
remain unchanged. This PR does not update consumers, move tags, publish packages,
deploy the broker, or rename the GitHub App.
