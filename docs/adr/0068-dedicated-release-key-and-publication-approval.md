# ADR 0068 - Dedicated release signing key and publication approval

Status: Proposed
Date: 2026-10-08
Owners/Reviewers: Maintainers
Related work: user-requested release Marshal automation; PR #229 preparation

## Context

Release preparation already generates a receipt-bound version/docs commit on
main and waits for all exact-head CI jobs. The owner currently signs its tag
locally. The requested automation must keep personal SSH keys out of Actions
and preserve Mo's final npm/PyPI publication approvals. An approval click is an
authorization record, not a cryptographic tag signature or package attestation.

## Decision

Add an explicit `sign_tag=true` owner/main-only preparation dispatch option.
After generated-main CI succeeds, a separate job imports a dedicated GPG
signing key into a disposable keyring, signs an annotated immutable `vX.Y.Z`
tag on that exact prepared SHA, verifies the full pinned fingerprint locally,
pushes only the new tag object, and requires GitHub's existing verified-tag
identity check to agree with the exact tag object and commit.

Use a passphrase-protected RSA-4096, signing-only primary key, expiring in one
year. Register its public key on Mo's existing GitHub account with an already
verified account email. Its display identity is ReviewSensei Release Marshal;
the key is dedicated to automation and never uses or imports a personal SSH or
GPG key. GitHub's user GPG-key registration API is the supported identity
registration path; the App installation is only the transport identity and
does not acquire user-account GPG permissions. A separate machine account
could be a future explicitly reviewed identity migration, not an implicit
extra account required for this setup.

The encrypted private-key export and passphrase live only as two secrets in
the existing `release-docs-main` main-only environment. Repository variables
pin the public fingerprint and tagger email. The signing job explicitly maps
those secrets only into its signing step, runs reviewed dispatch helpers and
validates a separate generated-source checkout. No released source helper is
executed with the signing key. GPG runs in its temporary keyring with inherited
Git configuration disabled, explicit key selection and no hooks. Commands
capture diagnostics without printing private data; key material is neither
uploaded as an artifact nor cached. The temporary agent and keyring are cleaned
on normal success/failure; the hosted runner is disposable on cancellation.

The existing dedicated App remains repository-only Contents write, Actions
read and Metadata read, with no Workflows, Administration, Packages or account
permissions. Its short-lived token triggers Release on tag push, unlike
`GITHUB_TOKEN`. The existing isolated main PR-rule exception remains required;
this change adds no tag exception or rule mutation. Existing tags are rejected
even on retries. No force push, remote deletion or movable-channel promotion
is implemented. A dropped push response reconciles only the exact new verified
tag object; otherwise the operator inspects the existing Release run.

Before signing and again before builds, require the unchanged `npm` and `pypi`
environments to have Mo as their sole required reviewer. Keep both environment
jobs and Trusted Publishing identities unchanged. GitHub release creation now
waits for both approved package publications, and successful Release completion
remains the prerequisite for Pages and released-doc snapshot writeback.

## Qualification, setup and recovery

The user runs the reviewed setup script locally. It verifies owner account,
verified email, main-only environment and publication reviewer metadata before
generating any key. It creates an exclusive private backup outside all Git
checkouts, registers only the public key, uploads private values from files on
stdin, and reads back public metadata and secret names. It never prints key or
passphrase bytes, uses a dedicated GnuPG home and leaves personal keyrings alone.
An interrupted upload resumes the exact manifest/key; different identities or
an incomplete generation backup stop instead of overwriting or rotating.
Keep the backup, including the GnuPG-generated revocation certificate, in
encrypted storage. The key and passphrase are co-accessible to the signing job;
passphrase protection does not isolate them from a compromised trusted runner.

Complete source review, actual App readiness, readers-first rollout/canary and
registry/environment qualification before choosing `sign_tag=true`. The default
remains false for safe setup/manual handoff. Receipt, all versions/generated
docs, all fifteen latest CI jobs, fresh main, unused GitHub/PyPI/npm versions,
registered key/email and publication reviewers are rechecked before tag push.
GitHub has no transaction spanning main, CI, environment settings and a tag
push: source can advance just after the final read. The tag still binds the
chosen immutable qualified SHA; Release independently checks its receipt,
ancestry, CI and signature rather than following a moving main/tag ref.

Signature/registration outages fail closed. If a pushed tag is unverified,
hold publication and diagnose registration without deleting/moving it; use an
explicitly reviewed higher version if that tag cannot be qualified. Once a tag
exists, retry only its original Release jobs/artifacts. Key rotation/revocation
requires an explicit owner operation and fresh public-identity qualification;
the setup script never performs implicit rotation. Neither key registration nor
publication approval establishes provider/reader readiness or promotes `v5`.

## Validation

Offline tests cover exact-SHA signing, pinned-key/UID selection, wrong/expired
signatures, unregistered emails, existing-tag refusal, changed main/CI/receipt,
missing/changed approval gates, dropped-response reconciliation, private
diagnostic suppression, one-key setup retry and file/stdin secret upload. They
use synthetic placeholders and mocked credential/signing tools; they generate
no real key, tag or publication. Run full CI and independently review the setup
and workflow before user-run registration. First live signing requires a
separate approved release dispatch; synthetic tests do not prove GitHub verified
a new key or a package was published.

## Sources

- [GitHub signature verification](https://docs.github.com/en/authentication/managing-commit-signature-verification/about-commit-signature-verification)
- [GPG registration API and user scopes](https://docs.github.com/en/rest/users/gpg-keys)
- [Verified GPG email requirement](https://docs.github.com/en/authentication/managing-commit-signature-verification/associating-an-email-with-your-gpg-key)
- [Annotated-tag API verification fields](https://docs.github.com/en/rest/git/tags)
- [App-token workflow triggers](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow)
- [Publication environment approval](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/review-deployments)
- [GnuPG quick key generation and export](https://www.gnupg.org/documentation/manuals/gnupg/OpenPGP-Key-Management.html)
