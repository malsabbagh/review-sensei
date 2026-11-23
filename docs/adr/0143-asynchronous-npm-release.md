# ADR 0143 — Independent npm qualification and Python-qualified v5

Status: Proposed
Date: 2026-11-23
Decision owner: Release maintainer

## Context

ADR 0100 separated the tag-triggered publishers. Preparation and signing still
consulted six npm registry versions, the npm reviewer policy and the combined CI
result. A waiting or failed native job could therefore prevent Python from
receiving its signed tag. The standard operator procedure also inherited an
account lookup from the one-time publisher migration.

## Decision

The Marshal qualifies the exact reviewed and generated commits through their
latest same-repository push/main CI. It requires every Python/platform lane
and shard. The explicitly named npm/native matrix and its combined branch
aggregate are evaluated by the npm publisher. Unclassified jobs remain
blocking. Branch admission continues to require the full aggregate.

Core and hosted publication retain protected approvals, signed receipt and
artifact/provenance checks. The signed release payload still includes hashes
of every prepared file, including synchronized wrapper version metadata. The npm
workflow waits for full CI, checks all six package manifests and uses the
original attested bundle. Operator account access is unnecessary for ordinary
releases. Registry-side OIDC authorization, protected npm approval, platform-first
publication, byte/integrity readback and installed consumers govern its outcome.
A registry denial or unclassified npm exit holds the original run. A recognized
transport failure can be reconciled against the exact attested bytes.
Account inspection during a publisher migration remains a separate owner action.

The signed v5 owner procedure verifies the old tag object, the proposed
workflow source, both PyPI publications and the automatic installed CLI run for
that exact hosted attempt. It checks the original GitHub gate and all four
installed receipts against the current source, versions and wheel digests before
a leased push. Post-move validation, reader/canary qualification and owner
approval still apply. Pages continues to require both distribution channels.

## Consequences

A single signed engine tag starts both publisher runs. Native CI or registry
failure remains visible on the original npm run and can hold Pages. Python
qualification and v5 can complete independently. A missing Python shard,
artifact, provenance record, installed lane or current source identity holds
the corresponding Python or v5 operation. Release receipts give operators
exact identities for independent recovery.

## Validation and rollout

Tests exercise pending, failed and absent npm, mandatory Python jobs,
stale or mismatched installed archives, protected reviewers and npm OIDC denial.
The new automatic installed-run title identifies the hosted run and attempt
for future promotions. Deploy via the normal reviewed main branch. Existing
signed tags retain their original workflows and recovery paths.
