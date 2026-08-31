# GitHub App Registration

ReviewSensei supports an optional GitHub App identity for branded reviews and
installation-time setup pull requests. This document covers registration,
permissions, webhook configuration, key rotation, and revocation. The Python
library does not register a live App. The optional
[`deploy/cloudflare`](../deploy/cloudflare/README.md) package provides the
documented Worker ingress; operators still create the App in GitHub, provision
secrets, and approve the live deployment.

## Canonical Public Identity

Use the public ReviewSensei identity for all App-facing names, icons, links,
and setup pull request content:

- App name: **ReviewSensei**
- App description:

  ```text
  ReviewSensei opens reviewable setup pull requests for repository-owned GitHub Actions code reviews. You control the runner, provider credentials, and merge decisions.
  ```

- Homepage: `https://reviewsensei.dev`
- Public source/docs: `https://github.com/malsabbagh/review-sensei`
- Support: [`SUPPORT.md`](https://github.com/malsabbagh/review-sensei/blob/main/SUPPORT.md)
- Privacy and data handling: [`docs/data-handling.md`](https://github.com/malsabbagh/review-sensei/blob/main/docs/data-handling.md)
- Internal development repositories must not be presented as customer
  dependencies.

## Create The App

1. Open [GitHub App settings](https://github.com/settings/apps) and create a
   new GitHub App with the ReviewSensei name and icon you want to publish.
   GitHub documents the App creation flow in
   [Creating a GitHub App](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/creating-a-github-app).
2. Set the homepage to `https://reviewsensei.dev` and the setup URL to the
   public ReviewSensei repository or documentation at
   `https://github.com/malsabbagh/review-sensei`. GitHub's setup URL guidance
   is covered by the registration documentation above.
3. Leave the webhook disabled unless you have deployed a webhook ingress. If
   you use the Cloudflare package, follow its
   [deployment and secret steps](../deploy/cloudflare/README.md#configure-and-deploy),
   then configure the payload URL, content type
   `application/json`, and a webhook secret. ReviewSensei verifies
   `X-Hub-Signature-256` with that secret and rejects missing, malformed,
   invalid, or duplicate deliveries. See GitHub's
   [Validating webhook deliveries](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries)
   guidance for the signature format.
4. Use **selected repositories** rather than all repositories so installation
   scope stays explicit.

## Least-Privilege Permissions

Request only the permissions needed by the features you deploy:

| Permission | Access | Why |
| --- | --- | --- |
| `Metadata` | Read | Required by GitHub for App identity and repository metadata |
| `Contents` | Write | Required to create the setup branch and generated files |
| `Pull requests` | Write | Required to open the setup pull request and later App-identity review PRs |
| `Issues` | Write | Required only for opt-in top-level `@sensei` replies on pull-request conversations; capability tokens omit it for review and inline-reply operations |
| `Variables` | Write | Required to create visible repository defaults used by the generated workflow; GitHub payloads name this permission `actions_variables`; existing values are never overwritten |
| `Workflows` | Write | Required because setup always creates or updates generated files under `.github/workflows/` |

Do not request organization administration, secrets, checks, or
unrelated repository permissions unless a separate design explicitly requires
them. GitHub documents the permission model in
[Choosing permissions for a GitHub App](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/choosing-permissions-for-a-github-app).
For an existing installation, accept the updated permission request before
expecting the `new_permissions_accepted` setup delivery.

## Webhook Events

The setup bootstrap handles the following webhook events:

| Event | Actions | Behavior |
| --- | --- | --- |
| `installation` | `created`, `new_permissions_accepted` | Create a setup pull request and default repository variables for selected repositories when `contents`, `pull_requests`, `variables` (payload key `actions_variables`), and `workflows` are write |
| `installation` | `deleted`, `removed`, `suspended`, `unsuspend` | No setup writes |
| `installation_repositories` | `added` | Create setup pull requests and missing default repository variables for added repositories |
| `installation_repositories` | `removed` | No setup writes |

All other events are acknowledged as no-ops before setup processing. Suspended
installations never produce setup writes.

For the Cloudflare deployment, set the payload URL to
`https://<worker-subdomain>.workers.dev/github/webhook`; the package keeps the
raw body out of the Durable Object and validates the payload inside the Worker.

GitHub's canonical event reference is
[Webhook events and payloads](https://docs.github.com/en/webhooks/webhook-events-and-payloads);
ReviewSensei handles `installation` and `installation_repositories` deliveries.

## Webhook Secret And Signature Verification

Set the App webhook secret in the runtime environment as
`GITHUB_APP_WEBHOOK_SECRET` by default, or inject a custom
`WebhookSecretSource`. The verifier:

- reads webhook bodies with a bounded size limit;
- compares `X-Hub-Signature-256` with HMAC-SHA256 in constant time;
- rejects empty or missing webhook secrets before signature comparison;
- rejects unsupported events, malformed JSON, missing repositories, and
  duplicate `X-GitHub-Delivery` values;
- records only delivery metadata and never raw webhook bodies, secrets,
  installation tokens, or authorization headers.

## Key Rotation And Emergency Revocation

Rotate GitHub App private keys through GitHub App settings and redeploy the new
PEM to the runtime secret store. Delete the old PEM after the new key has been
verified end to end.

For emergency revocation:

1. Suspend or delete the installation in GitHub App settings, rotate the App
   private key, or rotate the webhook secret.
2. Clear the runtime token cache and stop new setup or review writes.
3. Revoke externally stored installation tokens from your application or
   secrets store.
4. Treat installation deletion, suspension, permission downgrade, and webhook
   signature failures as authorization boundaries before publishing anything.

## Setup Pull Request Contents

Generated setup pull requests contain only:

- `.github/workflows/review-sensei-review.yml` with immutable Action pins;
- `.github/workflows/review-sensei-uninstall.yml` with an immutable Action pin;
- `.github/review-sensei/config.yml` with local-first provider defaults;
- a short pull request body explaining the repository variables and that cloud
  mode requires `OLLAMA_API_KEY` as a repository secret.

The bootstrap creates these plain-text repository variables when they are
missing: `REVIEWSENSEI_PROVIDER_MODE=local`,
`REVIEWSENSEI_LOCAL_MODEL=qwen3.5:4b`, and
`REVIEWSENSEI_CLOUD_MODEL=deepseek-v4-flash:cloud`. It never creates a blank
secret or reads/writes `OLLAMA_API_KEY`; operators add that value through
Repository Settings when they opt into cloud mode.

To uninstall, run the generated **Remove ReviewSensei setup** workflow. It uses
the repository's built-in `GITHUB_TOKEN` to open a cleanup PR that deletes only
the generated workflow/configuration files. This is intentionally manual:
GitHub revokes the App installation token when the App is uninstalled, so the
App cannot reliably create a PR after that event. The cleanup workflow leaves
learnings, repository variables, and secrets untouched for an operator to
review and remove separately.

Setup pull request content must reference only public ReviewSensei artifacts
and repositories. It must not present internal development repositories as
customer dependencies.

The setup service never includes private keys, installation tokens, webhook
bodies, authorization headers, or raw GitHub API responses in generated files,
PR bodies, logs, or errors.

### Updating an existing installation

Generated setup files carry a `ReviewSensei setup version` marker. After a
Worker deployment, the next setup-triggering installation delivery inspects the
three generated paths on the repository default branch. Older generated files,
including byte-exact v3 workflows with older SHA pins and managed v4 workflows
following an older tag, are refreshed through a migration pull request; current
files are left alone.
The bootstrap skips custom, malformed, or future-version files instead of
overwriting them. A deployment does not replay historical deliveries, so
accept the App's permission update (`new_permissions_accepted`) or remove and
re-add the repository to emit a fresh setup event. Merge the migration PR after
review; an uninstall/reinstall cycle is not required.

## Issue-64 setup-v4 lifecycle

Setup-v4 adds the public reusable workflow boundary. The operator-managed `v4`
git tag is an install-time update channel: the Worker resolves it and the
generated caller references the resulting full commit SHA. The caller requests
`contents: read`, `pull-requests: read`, `issues: read`, and `id-token: write`,
and defaults every write/artifact switch to `false`.
Automatic cloud pull-request review is GitHub-hosted and same-repository only;
local Ollama is manual or trusted-event-only on the operator's labeled runner.

The App may pass the customer-owned `OLLAMA_API_KEY` secret by name only. It
does not create, fetch, reveal, log, persist, or place the secret value in a
setup PR. Add the secret manually when enabling cloud mode.

| Delivery | Reconciliation | Write rule |
| --- | --- | --- |
| `installation.created` | Inspect every selected repository, including reinstall | Absent/legacy/v2/stale managed v3/v4 -> one setup-v4 PR; current v4 -> no-op |
| `installation.new_permissions_accepted` | Repeat the same selected-repository inspection | Reuse one open setup PR; custom/malformed/future -> zero writes |
| `installation_repositories.added` | Inspect only added repositories | At most one deterministic setup-v4 PR per repository |
| removed/deleted/suspended/unsupported | Do not reconcile setup | No setup write |

The Worker validates the configured tag, resolves it through GitHub, and
requires the result to equal `PUBLIC_WORKFLOW_SHA` before generation. It then
branches from the current default-branch head, writes only the three generated
paths, and never writes directly to the default branch. A failed or unavailable
capability, fork, insufficient permission, replay, or rate limit fails closed.
The OIDC broker requires that immutable SHA and can temporarily allow older
pinned SHA pairs through `PUBLIC_WORKFLOW_LEGACY_SHAS`.
Release ordering and rollback are documented in [`docs/installation.md`](installation.md)
and [`deploy/cloudflare/README.md`](../deploy/cloudflare/README.md).
