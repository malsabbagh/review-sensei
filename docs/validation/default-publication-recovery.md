# Default publication rollout and recovery

This repairs the actual default writer and caller. It does not activate another
publication host or qualify every possible 250-finding review.

## Reader-first rollout

1. Keep `github.publication_inventory: legacy`. Build and verify engine and GitHub
   adapter from the same reviewed source. Run publisher, command/reassessment,
   retained-recovery, installed wheel and extracted-sdist compatibility checks.
2. After separately authorized release, install updated readers in every caller:
   reusable hosted and local jobs, commands, delayed verification, human feedback,
   installed CLI and recovery jobs. Confirm the exact installed runtime. Old readers
   reject large indexes; they are not a safe fallback for an existing large review.
3. Only after those consumers are verified, obtain maintainer authorization to set
   trusted `github.publication_inventory: complete`. Both reusable lanes forward
   this value. This implementation leaves repository configuration on `legacy`.
4. Roll back new emission to `legacy` if necessary, retaining the updated readers.
   Do not downgrade readers while large reviews remain in active histories.

The complete profile preserves every finding and full explanation within the
existing 65,536-byte review, individual-item, future-assessment and path bounds.
Compact 250 entries can fit; arbitrary rich 250 entries need not. Overflow remains
blocked and retains the validated result when retention is enabled.

## Recover without inference

With `github.operation_entry: enabled`, the default application retains only the
final validated transaction-bound ReviewResult in strict-owned PR result parts.
`github.artifacts: none` still disables diagnostics containing reviewed diff and
trusted contexts. Those contexts remain required from the trusted caller.

Run the installed GitHub adapter against the same repository, numeric repository
ID, PR, base/head, trusted configuration/evidence documents and independently
selected session ledger. Use a read-only metadata token through `GITHUB_TOKEN` or
`GH_TOKEN`; mutations continue through the existing broker. For example:

```sh
review-sensei-github github review --recover-retained \
  --repository OWNER/REPO --repository-id REPOSITORY_ID --pull-request PR \
  --head-sha EXACT_HEAD --base-sha EXACT_BASE --base-branch main \
  --diff trusted.patch --configuration-context trusted-configuration.json \
  --evidence-context trusted-evidence.json --github-session-ledger \
  --publication-inventory complete --allow-write --enable-review
```

Use `--session-ledger PATH` instead for an existing serialized local ledger.
Verification-round admission still requires the original trusted
`--admission-context`; retained bytes do not recreate or authorize it. Keep the
ordinary accepted-operation guard enabled. No provider is constructed by this
recovery path. One fresh publication-attempt budget spans all its phases; it does
not reset at each read or publication step.

Missing, duplicate, malformed, foreign-owned, foreign-PR, stale-head, mismatching
configuration or digest parts refuse. A failed/interrupted initial write can
leave an unconfirmed durable phase. A fresh owner reconciles complete owned parts;
an incomplete or absent set cannot be blindly recreated. This hold requires
operator investigation. If the exact original result and trusted contexts are
available, the existing explicit `operation_entry: disabled` rollback can publish
those original bytes through ordinary validation; it is not a provider retry or
permission bypass. Do not change this switch merely to clear a genuine finding.

This serial crash fence is not an atomic distributed lock. Concurrent REST comment
writers remain an unqualified condition. Large feedback histories can still exhaust
the shared 60-second, 60 ordinary / 64 total dispatch budget and remain blocked.
No silent truncation, cap raise or older clean-review fallback is permitted.

## PR #316 incident boundary

At head `f59edcc89d54a72ca12d97300e69463417646bfd`, the provider run completed but
the deployed publisher rejected its inventory. No full result parts or diagnostics
artifact survived. The 77-record baseline contains identities, not all original
explanations, and cannot reconstruct the result. This repair prevents subsequent
loss; it cannot recover that old payload. Do not rerun its provider merely to test
publication. Verify the repaired installed path first and request any separate
future inference authorization afterward.
