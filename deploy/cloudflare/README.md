# ReviewSensei Cloudflare Worker package

This package deploys the optional ReviewSensei GitHub App installation
bootstrap without a Cloudflare Container. The Worker handles webhook
validation, GitHub App authentication, and setup pull requests directly. A
SQLite-backed Durable Object stores only bounded delivery metadata.

It is not a hosted review engine: customer repositories still run the review
workflow and provider compute in their own GitHub Actions setup.

## Runtime shape

```text
GitHub App webhook
    -> Worker /github/webhook
       -> bounded HMAC gate and payload validation
       -> DeliveryLedger (SQLite Durable Object)
       -> Web Crypto RS256 GitHub App JWT
       -> installation token and idempotent setup pull request
```

The Worker never stores raw webhook bodies, installation tokens, private keys,
diffs, provider output, or GitHub API response bodies. The one-hour ledger
retention window suppresses near-term redelivery; the existing setup branch and pull request
provide longer-lived setup idempotency.

## Free-tier requirements

The Worker-only package is compatible with the Cloudflare Workers Free plan.
It does not declare `@cloudflare/containers`, a Container image, or a Container
Durable Object binding. No Docker installation or Workers Paid plan is needed
for this package.

Cloudflare's current Free limits are documented in the official pricing pages:

- [Workers pricing](https://developers.cloudflare.com/workers/platform/pricing/)
- [Durable Objects pricing](https://developers.cloudflare.com/durable-objects/platform/pricing/)

The relevant limits are 100,000 Worker requests/day, 100,000 Durable Object
requests/day, 13,000 Durable Object GB-s/day, 5 million SQLite row reads/day,
100,000 row writes/day, and 5 GB of SQLite data. Limits reset at 00:00 UTC;
exceeding a Free quota makes that operation fail instead of creating an
overage charge. The Worker, ledger, and GitHub API calls are still subject to
GitHub rate limits and the configured App permissions.

## Prerequisites

- A Cloudflare account on the Workers Free plan.
- Node.js supported by the installed Wrangler release.
- A registered GitHub App with the setup permissions and installation events
  documented in [`docs/github-app-registration.md`](../../docs/github-app-registration.md).
- The App's webhook URL set to the deployed Worker URL and JSON content type.

The App needs `Contents: write`, `Pull requests: write`, and `Variables: write`
repository permissions. `Variables: write` lets the bootstrap create the
visible `REVIEWSENSEI_*` defaults without touching secrets. GitHub's webhook
and installation-token payloads expose this UI permission as
`actions_variables`; the Worker accepts both names. GitHub may also require
`Workflows: write` when the App creates a workflow file; request it only when
the target repositories need it. Enable the
`Installation` and `Installation repositories` events.

If the App was already installed before `Variables: write` was added, accept
the updated permissions in the repository installation settings. GitHub then
sends `installation.new_permissions_accepted`; that delivery is the retryable
setup trigger and will create the missing variables and setup PR.

## Configure and deploy

From this directory:

```bash
npm ci
npx wrangler login
npx wrangler secret put GITHUB_APP_ID
npx wrangler secret put GITHUB_APP_PRIVATE_KEY
npx wrangler secret put GITHUB_APP_WEBHOOK_SECRET
npx wrangler deploy
```

`GITHUB_APP_ID` is the numeric App id. `GITHUB_APP_PRIVATE_KEY` is the PEM key
issued by GitHub; paste it exactly as provided. `GITHUB_APP_WEBHOOK_SECRET`
must exactly match the secret in the GitHub App webhook settings.
`GITHUB_API_URL` is optional and defaults to `https://api.github.com`; use a
non-secret Worker variable only for a GitHub-compatible API endpoint you
operate.

After deployment, set the GitHub App webhook URL to:

```text
https://<worker-subdomain>.workers.dev/github/webhook
```

The endpoint accepts `POST` only. `GET /healthz` is a liveness check and does
not verify GitHub App configuration.

## Local development

Create a local-only `deploy/cloudflare/.dev.vars` file (it is ignored) with:

```text
GITHUB_APP_ID=123456
# Put the PEM value here locally using Wrangler's quoted multiline dotenv syntax.
# Never commit the real key or this local-only file.
GITHUB_APP_PRIVATE_KEY="REPLACE_WITH_LOCAL_PEM_VALUE"
GITHUB_APP_WEBHOOK_SECRET=replace-me
```

Then run:

```bash
npm run dev
```

Wrangler emulates the Worker and Durable Object locally. Do not commit a real
private key or webhook secret, and do not place credentials in generated setup
files or logs.

## Delivery and retry behavior

The Durable Object claim is a five-minute lease. A successful delivery is
retained as accepted for one hour. A setup or GitHub API failure releases the
claim so GitHub can retry; an expired lease is also reclaimable. A delivery id
received with a different body digest is rejected as a conflict.

The Worker validates the signed payload before any GitHub API call. It creates
one setup branch and pull request per selected repository, checks for an open
setup pull request before writing, and rechecks after branch creation to close
the race between concurrent deliveries. Insufficient App permissions produce
a skipped result without writing to the repository.

When setup permissions are available, the Worker also creates the missing
repository variables `REVIEWSENSEI_PROVIDER_MODE=local`,
`REVIEWSENSEI_LOCAL_MODEL=qwen3.5:4b`, and
`REVIEWSENSEI_CLOUD_MODEL=deepseek-v4-flash:cloud`. Existing values are left
unchanged. `OLLAMA_API_KEY` is a repository secret that the operator must add
manually when switching the provider mode to `cloud`; the Worker never creates
blank secrets.

The generated setup PR includes a manual **Remove ReviewSensei setup** workflow.
Running it uses the repository `GITHUB_TOKEN` to create a cleanup PR for the
generated workflow/configuration files. An App installation token is revoked
when the App is uninstalled, so the Worker cannot reliably create that PR from
an `installation.deleted` webhook. Learnings, variables, and secrets are left
for explicit operator cleanup.

### Existing installations and setup migrations

Generated files are marked `ReviewSensei setup version: 2`. On a new setup
delivery, the Worker reads only the three generated paths from the repository's
default branch, bounded to 128 KiB each. An older ReviewSensei setup (including
the original unmarked workflow/configuration) causes the existing setup branch
to be refreshed and a migration PR to be opened. A current setup is a no-op.
If a known path contains custom content, a malformed marker, or a future setup
version, the Worker skips it without overwriting the file and reports the
`skipped_unknown_setup` outcome in its internal result.

Deploying the Worker does not replay old webhook deliveries. After deployment,
trigger a fresh `installation.new_permissions_accepted` delivery by accepting
the App permission update, or remove and re-add the repository to the App. The
resulting migration PR is the only repository write; merge it after review.
No uninstall or manual deletion is required, and repository learnings and
existing variable/secret values are preserved.

## Rollback and operations

Deploy from a known Git commit. To roll back, redeploy the previous Worker
commit with the same Wrangler configuration, then verify `GET /healthz` and
send a signed test delivery from GitHub. Do not delete the `DeliveryLedger`
namespace during rollback; its migration and one-hour accepted-delivery state
are part of the delivery contract. Rotate secrets with `wrangler secret put`
and redeploy.

This repository does not run `wrangler deploy` or register the GitHub App as
part of CI. Those are operator-owned external writes and require the target
Cloudflare account, GitHub App, and production approval.
