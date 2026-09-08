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

GitHub Actions OIDC
    -> Worker /github/token
       -> exact reusable-workflow claims and server-side installation lookup
       -> BrokerLedger (hashed replay/rate identities only)
       -> one capability-scoped installation token
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

The App needs `Contents: write`, `Pull requests: write`, `Variables: write`,
and `Workflows: write` repository permissions. `Pull requests: write` covers
reviews, inline replies, and top-level replies on pull-request conversations;
the App does not need `Issues: write`. `Variables: write` lets the bootstrap
create the visible `REVIEWSENSEI_*` defaults without touching secrets. GitHub's
webhook and installation-token payloads expose this UI permission as
`actions_variables`; the Worker accepts both names. `Workflows: write` is
required because setup always writes generated workflow files. Enable the
`Installation` and `Installation repositories` events.

If the App was already installed before `Variables: write` or `Workflows: write` was added, accept
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

Before the first production deploy, replace the workflow placeholder in
`wrangler.jsonc` (or set equivalent dashboard variables):

```text
PUBLIC_WORKFLOW_TAG=v4
```

`PUBLIC_WORKFLOW_TAG` is the v4 update channel. During setup the Worker
resolves the tag through GitHub's public Git ref advertisement before writing
generated callers; it retains the REST ref lookup as a fallback. This avoids
GitHub's low anonymous REST quota while preserving the tag-to-commit check.
The OIDC broker resolves the same tag at capability exchange time and requires
the runtime workflow SHA to match that resolution. Moving the tag is therefore
an operator-controlled release action; protect the tag and publish the
reviewed snapshot before moving it. A Worker deploy does not create or move
the public tag.

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

Generated files are marked `ReviewSensei setup version: 4`. On a new setup
delivery, the Worker reads only the three generated paths from the repository's
default branch, bounded to 128 KiB each. An older ReviewSensei setup (including
the original unmarked workflow/configuration) causes the existing setup branch
to be refreshed and a migration PR to be opened. A current setup is a no-op.
The v4 caller follows `malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml`
at the configured `PUBLIC_WORKFLOW_TAG` (default `v4`); invalid values or an
unavailable tag fail closed before any setup write.
If a known path contains custom content, a malformed marker, or a future setup
version, the Worker skips it without overwriting the file and reports the
`skipped_unknown_setup` outcome in its internal result.

The legacy catalog contains the union of byte-exact released pre-marker and
setup-v2 Worker/Python artifacts. Current-v3 recognition is byte-exact for any
valid public workflow SHA so v3 clients can migrate. Current-v4 recognition is
byte-exact for the configured tag; a review workflow that follows another valid
tag is managed stale content and is migrated. Edited or inconsistent content
remains unknown. Migration uses a create-only branch named
`review-sensei/setup-v4-<base12>-<tag>`. An existing branch is reusable only
when its parent is the named base SHA, its author is the ReviewSensei App, its
exact generated content is current, and its comparison changes generated paths
only. Any pre-existing or concurrent mismatch returns
`skipped_branch_conflict`; setup never force-moves a ref.
The broker accepts only the configured v4 tag ref and its current runtime SHA,
so a stale caller must merge its tag-following migration PR before it can
obtain a new cloud capability.

Deploying the Worker does not replay old webhook deliveries. After deployment,
trigger a fresh `installation.new_permissions_accepted` delivery by accepting
the App permission update, or remove and re-add the repository to the App. The
resulting migration PR is the only repository write; merge it after review.
No uninstall or manual deletion is required, and repository learnings and
existing variable/secret values are preserved.

### Setup-v4 execution and broker boundary

Cloud review and `@sensei` reply operations use GitHub-hosted compute. Local
review and reply operations use the labelled self-hosted runner. Both provider
modes support automatic/manual review, learning proposals, optional artifacts,
and bounded multi-turn PR conversations. Cloud mode sends the selected review
or conversation context to Ollama Cloud; local mode keeps it on the configured
local service. An authorized mention receives a temporary 👀 reaction until the
reply or another terminal outcome.
Generated write and artifact switches default to false. When automatic review
and GitHub writes are enabled, only a validated clean exact-head review whose
bounded final thread sweep is fully resolved emits `APPROVE`. Findings, open
threads, ineligible pull requests, and `@sensei` replies remain ordinary
`COMMENT` events, and the existing per-head marker prevents duplicate writes.
The generated caller may pass `OLLAMA_API_KEY: ${{ secrets.OLLAMA_API_KEY }}` by
name only; the App never creates, retrieves, logs, persists, or reveals that
secret value.

The isolated `POST /github/token` route accepts a bounded OIDC exchange for
`review_publish`, `inline_reply`, `issue_reply`, or `learning_write`. It
resolves the configured workflow tag, verifies its tag ref and runtime SHA,
and checks repository identity; it rejects
forks and unsupported runner environments/events, resolves the installation server-side,
and claims replay/rate state in `BrokerLedger`. A bounded pre-auth admission
check runs before JWKS work, public JWKS reads are cached for five minutes with
concurrent refresh coalescing, and a verified assertion is claimed before any
GitHub repository or installation lookup. The route is no-store and has no
CORS contract. `PUBLIC_WORKFLOW_TAG` is the only workflow-channel Worker
variable and must be set before deployment.

For a failed exchange, stream the Worker logs while reproducing the workflow:

```bash
npx wrangler tail reviewsensei-github-app --format json
```

`github_broker_failed` entries contain only a normalized `error_code`, the
bounded capability name, and (when Cloudflare supplies one) the `cf_ray` request
identifier. `oidc_*` codes identify assertion validation, `broker_*` codes
identify policy, replay, or installation decisions, and `github_*` codes
identify GitHub API or App failures. A code such as
`github_capability_issue_failed_422` preserves only GitHub's HTTP status; raw
tokens, claims, response bodies, and exception text are never logged or returned.

## Rollback and operations

Deploy from a known Git commit. To roll back, redeploy the previous Worker
commit with the same Wrangler configuration and migration history, then verify `GET /healthz` and
send a signed test delivery from GitHub. Do not delete the `DeliveryLedger`
or `BrokerLedger` namespace during rollback; their migrations and accepted
identity state are part of the delivery contract. Rotate secrets with `wrangler secret put`
and redeploy.

This repository does not run `wrangler deploy` or register the GitHub App as
part of CI. Those are operator-owned external writes and require the target
Cloudflare account, GitHub App, and production approval.
