# Dedicated Release Marshal signing

The Marshal can sign the qualified prepared commit with a dedicated release
GPG key. Mo still approves the `npm` and `pypi` environment jobs before either
registry publishes. That approval records authorization; the GPG key creates
the cryptographic tag signature, and Trusted Publishing creates package
provenance/attestations. These are separate records.

## User-run setup

Run the reviewed script yourself on macOS/Linux. It targets only
`malsabbagh/review-sensei`, registers the public key on the authenticated
`malsabbagh` account, and stores the two private values only in its existing
main-only `release-docs-main` environment. No new App/account or personal SSH
key is required. The App's existing private key is a different credential and
is not read or uploaded by this script.

Prerequisites: GnuPG 2.1+, Python 3.11+, GitHub CLI, existing repository-owner
authentication, a verified GitHub email, reviewed source, and the existing
main-only environment plus Mo as sole required reviewer on both npm/PyPI.
GnuPG is not currently installed on the connected Mac; install it yourself
with `brew install gnupg` if needed. Grant the local setup CLI only the needed
user scopes with `gh auth refresh --hostname github.com --scopes write:gpg_key,user:email`
if they are missing. Existing owner repository authority is needed for
environment secret/repository variable uploads. No runtime App permission is
added. Review the requested OAuth scopes before confirming the CLI prompt.

From the reviewed checkout, one command performs setup:

```bash
python3 scripts/setup_release_signing.py --backup-dir "$HOME/.reviewsensei-release-signing"
```

It chooses the account's verified primary email; use `--email VERIFIED_EMAIL`
to select a different already-verified address. The address will be public in
signed tags and a repository variable. Reuse that explicit email on retries if
the account's primary email changes.

The script generates a **new** RSA-4096 signing-only GPG primary key, one-year
expiry and random passphrase inside a dedicated `0700` backup/keyring. It uses
exclusive `0600` files, never reads personal SSH keys or the default GPG home,
and uploads secret files through captured `gh secret set` stdin. Private
bytes never appear in arguments, stdout/stderr, chat, Git, artifacts or logs.
The only output is public fingerprint/email/destination/backup path and status.
Do not run with shell tracing, pipe key files to the terminal, paste them into
chat or place this backup in a checkout. Keep the complete backup on encrypted
storage or in a password manager, including `gnupg/openpgp-revocs.d/` for
revocation. The encrypted export and its passphrase are both retained locally;
private filesystem permissions do not replace encrypted storage.

| Destination | Name | Contents |
| --- | --- | --- |
| `release-docs-main` environment secret | `RELEASE_SIGNING_PRIVATE_KEY` | Passphrase-protected armored dedicated GPG private export |
| Same environment secret | `RELEASE_SIGNING_PASSPHRASE` | Generated passphrase |
| Repository variable | `RELEASE_SIGNING_FINGERPRINT` | Full public uppercase GPG fingerprint |
| Repository variable | `RELEASE_SIGNING_EMAIL` | Already-verified account email used by the tagger |
| GitHub account GPG keys | ReviewSensei Release Marshal | Public key only |

Rerun the same command after an interrupted registration/upload: it reuses
the exact completed backup and does not create another key. Existing different
variables/secrets or an incomplete key-generation directory stop setup for
local inspection. Keep the backup; do not delete it to force a retry or silently
replace a configured key. Rotation is a separate reviewed owner operation.

## Public verification and release

After setup, this read-only command checks public identity, registered verified
email, secret **names** and approval/branch policies. It reads no private key
file or secret value and can be run by the assistant with existing owner access:

```bash
python3 scripts/setup_release_signing.py --verify-only
```

This is metadata verification, not proof of a live signed tag. Complete
[App readiness and the existing readers-first release qualification](releasing.md#prepare-main-before-signing-and-building),
then explicitly request automation in an owner/main dispatch:

```bash
gh workflow run prepare-release.yml --repo malsabbagh/review-sensei --ref main \
  -f version=0.6.17 -f release_date=2026-10-08 -f sign_tag=true
```

Use the intended release version/date, not these example values blindly.
`sign_tag=false` (default) retains a manual handoff with no signing-secret use.
The signing job waits for prepared CI and refreshes receipt/main/CI/unused
versions, narrow App identity, public-key registration and publication reviewer
gates. It selects only the dedicated fingerprint, locally verifies that
signature, pushes a new annotated tag without force, and requires GitHub's
verified object/source readback. The App token triggers the existing Release
workflow; personal owner credentials are not stored in Actions.

Command failures report fixed operation names and exit codes, never raw command
arguments or stdout/stderr. Cleanup attempts agent shutdown, local tag removal
and temporary keyring removal independently. If signing already failed, cleanup
warnings preserve that original error. If signing succeeded but cleanup fails,
the run fails rather than reporting success. Inspect the emitted public tag
object/source SHA before recovery; cleanup never deletes a remote tag.

Inspect exact source/tag SHA, GPG fingerprint and Release builds before
approving the two publish jobs. GitHub assets wait for both publications;
successful Release is still required for released docs/Pages. If a tag exists,
never rerun signing or move/delete it: recover its original Release jobs and
artifacts. Signing failures or a missing approval gate hold publication. If
rules reject the App's tag write, inspect them separately; no broad tag bypass
is introduced. `v5`, Worker, provider qualification and consumer migration
remain separate operator actions.

See [ADR 0068](adr/0068-dedicated-release-key-and-publication-approval.md) for
identity choice, credential lifetime, race limits and recovery.
