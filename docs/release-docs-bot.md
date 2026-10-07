# Dedicated release documentation App

This setup enables the owner-approved automatic main update after a successful
release. Keep **v0.6.17 held** until setup and the real App readiness check below
succeed. Implementation PR #227 still needs ordinary independent review before
merge; approval of this App exception does not waive implementation review.

## Register and install the dedicated App

Register a new App under [the malsabbagh owner settings](https://github.com/settings/apps/new),
for example **ReviewSensei Release Docs**. Use the repository URL as its homepage,
disable webhooks, and make it private (installation only on this account).
Install it with **Only select repositories → review-sensei**. Verify that the
entire installation selects this one repository, not all owner repositories.
Do not reuse or widen the existing review App, its Worker/broker or its keys.

Grant exactly these repository permissions, with no account permissions:

| Permission | Level | Purpose |
| --- | --- | --- |
| Contents | Read and write | Read verified release source and commit generated docs to main |
| Actions | Read-only | Qualify successful release runs and their retained artifacts |
| Metadata | Read-only | GitHub's required metadata access and active-rule checks |

Leave Workflows, Administration, Pull requests, Issues, Pages and Packages at
no access. A numeric App ID and slug are public setup identifiers; the private
key and installation tokens are secrets.

GitHub does not restrict Contents write by file path. It permits repository
content/ref/release operations, subject to existing rules. The trusted workflow
enforces the generated-docs allowlist and never updates source, workflows, tags
or release objects. The App receives no permission to edit workflows or rules.
A compromised key therefore remains a repository-content risk; the main-only
environment and ordinary independent source review protect its trusted use.

## Store the key directly in GitHub

Use the repository's [release-docs-main environment settings](https://github.com/malsabbagh/review-sensei/settings/environments).
Its deployment branch policy must allow **only the branch `main`**, with no tag
policy or wildcard. No per-release approval is required for this environment;
Pages keeps its existing separate deployment environment and reviewers.

Generate a private key in the dedicated App's settings. Enter its PEM value
directly as environment secret **`RELEASE_DOCS_APP_PRIVATE_KEY`**. Add environment
variable **`RELEASE_DOCS_APP_ID`** containing the numeric App ID.
Do not store either in repository-wide secrets, upload the PEM to an assistant,
paste it into chat, or commit it. Keep any downloaded key file outside the repo
and handle rotation in the App and environment settings.

The pinned GitHub-maintained token Action authenticates with that numeric ID
and key, explicitly narrows each token to this repository and the three listed
permissions, masks it, and revokes it at job completion. Installation tokens
also expire after one hour. The writeback job has a twenty-minute timeout and
does not persist Git credentials. A long-lived private key is required to mint
those short-lived tokens; it is held only in the main-only environment.

## Apply the approved exception without weakening source protection

With the owner's existing repository-administration authentication, run the
configuration helper from the reviewed implementation. It does not accept,
read, upload or transmit the private key. Supply only the public App ID/slug:

```bash
python scripts/configure_release_docs_bot.py --app-id APP_ID --app-slug APP_SLUG
python scripts/configure_release_docs_bot.py --app-id APP_ID --app-slug APP_SLUG --apply
```

The first command prints the exact proposed ruleset payloads without writes.
The second verifies the current policy and applies only the approved exception:

1. Create and read back an active main-only `main-reviewed-source` ruleset
   retaining the complete existing PR rule: one approval, stale approvals
   dismissed, code-owner review, last-push-independent approval, extra approval
   for unattributed changes, and squash-only merging. Its bypass actors are the
   pre-existing administrator role and this dedicated App, in `always` mode.
2. Only after that readback, remove the duplicate PR rule from existing ruleset
   `21068957`. Retain its deletion, non-force and linear-history rules, branch
   conditions and existing administrator actor unchanged. Give the App no
   bypass on this ruleset or any other active repository ruleset.
3. Read both policies back and check all other active rulesets. Retry is a no-op
   when the exact configuration already exists. Unexpected parameters, actors
   or concurrent owner edits stop setup; a failed second operation leaves the
   original PR requirement enforced. Never disable protections to recover.

This owner setup uses pre-existing administration authority; the runtime App
does not gain Administration access. Record the public App ID, installation
selection, environment branch policy and resulting ruleset IDs in the release
qualification. If another classic or inherited rule blocks direct writes,
hold the tag and review it; this procedure does not remove other protections.

## Verify real-token readiness before the first tag

After the implementation's reviewed merge and owner setup, dispatch
**Check release documentation bot** on main:

```bash
gh workflow run release-docs-bot-check.yml --ref main
```

Wait for that exact workflow SHA to reach terminal success. It only reads App
identity, token-visible repositories and main rules; it does not qualify a
release, download artifacts, commit, push, publish, deploy or modify settings.
The token-visible repository list must contain exactly this repository. That
proves the minted token's scope; the owner's installation-selection verification
above proves the whole App installation is also repository-only.

GitHub may omit bypass actor lists from metadata readers. The guard instead
requires its effective `current_user_can_bypass` result to be `always` for the
unchanged isolated PR rule and `never` for all applicable main non-PR rulesets.
When actor lists are visible it also validates them. Any missing/unknown result,
mixed PR/core ruleset, broad App permission, wrong repository or core-rule bypass
fails closed. Do not add Administration permission as a workaround. The real
probe must confirm this API contract for the narrow token before readiness is
claimed. Runtime checks applicable main rulesets; owner setup verifies actor
isolation across all active repository rulesets.

Only then rebase release PR #226 onto main containing the automation and
requalify its exact head. Follow [the release procedure](releasing.md#tag-driven-documentation-starting-with-0617)
for its reviewed merge and signed immutable `v0.6.17` tag. Generated docs commits
use the dedicated bot identity. App pushes run ordinary main CI, while Release
remains tag-triggered and Pages listens only to Release completion or explicit
release-run retry, so a docs commit does not start another release/docs cycle.

For recovery, retain the existing cutoff, repair the reviewed App/environment
configuration and retry the same successful release run. Key rotation needs a
fresh readiness check. To revoke the exception, remove only the App actor from
`main-reviewed-source` and remove/revoke its environment key; ordinary PR and
core protections remain active. Never move a released tag.

Contracts: [GitHub token Action](https://github.com/actions/create-github-app-token),
[installation token scope](https://docs.github.com/en/rest/apps/installations#list-repositories-accessible-to-the-app-installation),
and [repository rulesets](https://docs.github.com/en/rest/repos/rules#get-a-repository-ruleset).
