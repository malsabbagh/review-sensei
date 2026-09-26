# ReviewSensei Cloudflare Worker package

This package deploys the optional ReviewSensei GitHub App installation
bootstrap without a Cloudflare Container. The Worker handles webhook
validation, GitHub App authentication, and setup pull requests directly. A
SQLite-backed Durable Object stores only bounded delivery metadata.

It is not a hosted review engine: customer repositories still run the review
workflow and provider compute in their own GitHub Actions setup. The Worker
does not dispatch reviews and does not invent a SHA-based concurrency key.
Hosted PR-scoped latest-wins admission is owned by the reusable workflow
`.github/workflows/review-sensei-run.yml`.

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

The App needs `Contents: write`, `Pull requests: write`, and `Workflows: write`
repository permissions. `Pull requests: write` covers reviews, inline replies,
and top-level replies on pull-request conversations; the App does not need
`Issues: write`. Setup provisions no repository variables, so the App needs no
`Variables` permission: no setup or capability token requests the retired
scope, a webhook delivery from an installation that still carries it is
handled exactly like one that does not, and an installation-token response
that still reports it fails closed. `Workflows: write` is required because
setup always writes generated workflow files. Enable the `Installation` and
`Installation repositories` events.

If the App was already installed before `Workflows: write` was added, accept
the updated permissions in the repository installation settings. GitHub then
sends `installation.new_permissions_accepted`; that delivery is the retryable
setup trigger and creates the missing setup pull request.

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
PUBLIC_WORKFLOW_TAG=v5
```

`PUBLIC_WORKFLOW_TAG` is the v5 update channel. During setup the Worker
resolves the tag through GitHub's public Git ref advertisement before writing
generated callers; it retains the REST ref lookup as a fallback. This avoids
GitHub's low anonymous REST quota while preserving the tag-to-commit check.
The OIDC broker resolves the observed public tag from the OIDC
`job_workflow_ref` at capability exchange time and requires the runtime workflow
SHA to match that resolution. During channel migrations the broker accepts both
`v4` and `v5` while `PUBLIC_WORKFLOW_TAG` remains the write channel for new
setup output. Moving the tag is therefore an operator-controlled release
action; protect the tag and publish the reviewed snapshot before moving it. A
Worker deploy does not create or move the public tag.

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

An install that selects more than one repository, and an installation
lifecycle event whose payload omits the repository list, is reconciled one
repository per alarm. The Workers Free plan allows 10 ms of CPU and 50
subrequests per invocation; reconciling several repositories inside the
webhook invocation exceeds those limits and GitHub records a 503. The webhook
claims the delivery, stores a bounded continuation cursor, arms the ledger
alarm, and only then returns 202. Each alarm reconciles one repository and
arms the next. The lease is refreshed on each step. Transient GitHub failures
retry that repository after one second, then five seconds. A fatal configuration
error, or a repository that still fails after retries, releases the claim and
retains an outcome record with the stable error code, failure count, and
repository slug for the one-hour retention window. Logs record the error code
and failure count only.

When setup permissions are available, the Worker creates the setup pull
request and no repository variables at all: the generated files are the whole
provisioned surface, and the only two product overrides are the optional
`REVIEWSENSEI_PROVIDER` (`inference.backend`) and `REVIEWSENSEI_MODEL`
(`inference.model`) Action variables an operator may set by hand. Cloud
credentials are repository secrets the operator must add manually per backend:
`OLLAMA_API_KEY`, `OPENROUTER_API_KEY`, or `OPENAI_API_KEY`; the Worker never
creates blank secrets.

The generated setup PR includes a manual **Remove ReviewSensei setup** workflow.
Running it uses the repository `GITHUB_TOKEN` to create a cleanup PR for the
generated workflow/configuration files, including the retired
`.github/review-sensei/config.yml` location. An App installation token is
revoked when the App is uninstalled, so the Worker cannot reliably create that
PR from an `installation.deleted` webhook. Learnings and secrets are left for
explicit operator cleanup.

### Existing installations and setup migrations

Generated files are marked `ReviewSensei setup version: 5`. On a new setup
delivery, the Worker reads only the three generated paths from the repository's
default branch, bounded to 128 KiB each. An older ReviewSensei setup (including
the original unmarked workflow/configuration) causes the existing setup branch
to be refreshed and a migration PR to be opened. A current setup is a no-op.
The setup-v5 caller follows `malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml`
at the configured `PUBLIC_WORKFLOW_TAG` (default `v5`); invalid values or an
unavailable tag fail closed before any setup write.
If a known path contains custom content, a malformed marker, or a future setup
version, the Worker skips it without overwriting the file and reports the
`skipped_unknown_setup` outcome in its internal result. The retired
`.github/review-sensei/config.yml` path is not part of the managed set: the
Worker neither reads nor writes it, and the generated uninstall workflow is
what retires it.

The legacy catalog contains the union of byte-exact released pre-marker and
setup-v2 Worker/Python artifacts. Current-v3 recognition is byte-exact for any
valid public workflow SHA so v3 clients can migrate. Current-v5 recognition is
byte-exact for the configured tag; a caller that follows another valid tag is
managed stale content and is migrated. Edited or inconsistent content remains
unknown. Migration uses a create-only branch named
`review-sensei/setup-v5-<base12>-<tag>`. An existing branch is reusable only
when its parent is the named base SHA, its author is the ReviewSensei App, its
exact generated content is current, and its comparison changes generated paths
only. Any pre-existing or concurrent mismatch returns
`skipped_branch_conflict`; setup never force-moves a ref.
The broker accepts the configured write-channel tag (`v5` by default) and,
during migration, also accepts `v4`. The runtime SHA must match the resolved
tag, so a stale caller must merge its tag-following migration PR before it can
obtain a new cloud capability.

Deploying the Worker does not replay old webhook deliveries. After deployment,
trigger a fresh `installation.new_permissions_accepted` delivery by accepting
the App permission update, or remove and re-add the repository to the App. The
resulting migration PR is the only repository write; merge it after review.
No uninstall or manual deletion is required, and repository learnings and
existing variable/secret values are preserved.

### Setup-v5 execution and broker boundary

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
The generated caller may forward `OLLAMA_API_KEY`, `OPENROUTER_API_KEY`, or
`OPENAI_API_KEY` from repository secrets by name only; the App never creates,
retrieves, logs, persists, or reveals any of those values.

The isolated `POST /github/token` route accepts a bounded OIDC exchange for
`review_publish`, `review_status`, `inline_reply`, `issue_reply`, or
`learning_write`. It
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

Capability and setup installation tokens require the returned permission map to
exactly match the requested map plus GitHub's mandatory `metadata: read`;
responses that omit metadata fail closed. `review_publish` alone may also
receive an unrequested `contents: read` because GitHub can return it to allow
repository-data access while publishing a review. The broker never requests
that permission, and `review_status`, `inline_reply`, `issue_reply`, and `learning_write`
reject it if it is returned. This is a narrow compatibility exception, not an
additional capability grant. The retired Variables scope is not in the
recognized permission set at all: setup provisions no repository variables, so
nothing requests it, and a token response that still reports it under either
GitHub spelling (`variables` or `actions_variables`) fails closed as an unknown
permission instead of being normalized and accepted.
Any unknown returned permission, unexpected level, or requested-level mismatch
also fails closed: the broker issues a token only after GitHub's returned map
matches the approved capability contract exactly. This can temporarily reject
an exchange while an App permission change is still reconciling; accept the
updated installation permissions and retry from a fresh workflow run rather
than accepting a downgraded or newly introduced permission automatically.

When GitHub introduces a documented implicit permission or a new capability
needs one, treat it as a broker-policy change: confirm the GitHub behavior,
assess the capability's minimum scope, add a capability-specific explicit
opt-in with accept/reject regression tests, and update this contract. Do not
broaden the generic adapter to tolerate unrecognized read or `none` grants.

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
