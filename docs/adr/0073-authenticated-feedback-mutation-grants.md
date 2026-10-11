# ADR 0073 - Authenticated feedback mutation grants

Status: Proposed
Date: 2026-10-10
Owners/Reviewers: Maintainers
Approved by: not applicable
Related issues: [#238](https://github.com/malsabbagh/review-sensei/issues/238), [#242](https://github.com/malsabbagh/review-sensei/issues/242), [#243](https://github.com/malsabbagh/review-sensei/issues/243), [#245](https://github.com/malsabbagh/review-sensei/issues/245)
Related PR: [#257](https://github.com/malsabbagh/review-sensei/pull/257)

## Context

The existing session grant authorizes a full review or a parsed maintainer
command. An explanatory human reply is a distinct source of authority. Treating
it as a command would skip the command contract; accepting its author name or
body digest from a runner would skip live GitHub authorization. Large feedback
also needs complete source selection, stable event identity and explicit
finding targets without placing comment bodies in credentials or diagnostics.

## Decision

Add a closed version-2 feedback attestation alongside the unchanged version-1
review and command attestations. The request carries D's metadata-only
`feedback-v1` authorization document and an exact mutation reference. Ordered
source identities, numeric actors, timestamps, body lengths and digests,
snapshot, full target IDs, selection digest and stable trigger event key remain
bound. The mutation reference binds operation, source, authority, execution,
inventory, reservation, root, generation, request, dispatch and original
control accounting. These fields are reference data until independently checked.

The optional `admission-dispatch` reason identifies a single checkpoint that
retains complete original admission and the first charged request together.
Both exact request and dispatch digests are required. Its grant has the same
source checks and one-use consumption as other mutations; the reason does not
enable coalescing, establish an original-attempt witness or increase a budget.

The Worker validates the original OIDC repository, run and reusable workflow
identity. It reloads the open PR and every selected source from the canonical
GitHub endpoints. Each source must belong to this PR and have an exact numeric
`User` actor with OWNER, MEMBER or COLLABORATOR association. The trigger's numeric
actor and login must match OIDC and retain a standalone ReviewSensei mention.
Full original UTF-8 bodies are loaded without clipping to recompute the exact
Python-compatible selection digest. Bodies remain transient authorization input;
credentials, ledger records and diagnostics contain metadata and hashes.

Issued grants are HMAC-signed. `BrokerLedger` is removed. Consuming verification remains.
A fresh OIDC assertion is required for each ordinary issuance. Version-1
canonical grant hashing remains unchanged. Version-2 hashing uses canonical JSON
with sorted object keys and preserved array order, matched by Python vectors.
The Python BrokerClient can charge every caller transport before dispatch using
the original control-budget callback. Worker-internal GitHub and JWKS transports
must also be measured and admitted by the eventual operation protocol.

## Qualification and remaining work

This implementation qualifies parsing, live source authorization and exact
one-use grant binding. It does **not** authenticate the caller's supplied root
or establish a durable original-attempt witness. A new grant cannot replenish
original accounting or prove that an earlier pre-root failure spent no budget.
The hosted richer route remains disabled until the owned-root, sealed-tail,
durable attempt, source fences and queue checkpoint composition is qualified.

Required follow-up evidence includes real production grant verification. `BrokerLedger` is removed. Remaining evidence includes fresh OIDC age checks, source edit/deletion and actor changes,
parent kills before and after consumption, whole-operation physical dispatches,
root ambiguity reconciliation and original-deadline restart. The current
transport lower bounds already exceed the original 64-dispatch envelope when
naively composed; component successes are not useful hosted capacity evidence.

## Consequences and alternatives

Feedback acquires explicit authority without changing command syntax, App
permissions or deployment bindings. The Worker now reads complete human comment
bodies transiently for authorization; it still receives no diff or provider
response. Endpoint and per-source bounds intersect the existing request limits,
so admission of 32 sources is not a guarantee that their complete lifecycle fits.
Numeric actor identity, exact byte digests and source order prevent normalization
or login-only substitutions from changing authority.

Relabeling a reply as a command, trusting runner-only source checks, omitting
broker-internal transport costs or increasing limits to make a fixture pass are
rejected alternatives. Original attempt recovery and receipt indexing require
separate reviewed protocols; this grant does not silently supply either.

## Validation and rollback

Shared Python/TypeScript synthetic vectors verify canonical selection and event
digests, long Unicode bodies and body-free credentials. Tests exercise actual
GitHub HTTP adapters, source/actor/snapshot mutations and pre-dispatch exhaustion.
Existing version-1 tests continue unchanged. Combined production IPC and hosted
fault qualification remains a release gate.

Rollback disables version-2 issuance and keeps version-1 readers and issuance.
New outstanding grants remain opaque, bounded and expiring; no queue inventory,
receipt or source tombstone is removed by rollback. Writer activation, deployed
reader migration and publication remain independent maintainer decisions.
