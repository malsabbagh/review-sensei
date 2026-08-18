# ADR 0006: Hosted GitHub App architecture

- Status: Accepted
- Date: 2026-08-10
- GitHub issue: #6
- Draft PR: #23
- Approved by: @malsabbagh via plan review
- Superseded by: open-source-first direction recorded in `docs/architecture.md`
  and the README; no replacement ADR is required for the current target.

> Superseded 2026-08-11 by product direction change: Code Sensei is
> open-source-first and users run Code Sensei in their own repositories,
> primarily through GitHub Actions. The hosted review service is not part of
> the current target. This ADR is retained for historical context only.

## Context

Code Sensei already has a provider-neutral `ReviewService`, validated
`ReviewResult`, trusted target/base context loaders, and a host-enforced
concurrency policy. The review core must remain independent of GitHub, queue,
database, and model-provider SDKs. Issue #6 needs an installable, hosted GitHub
App without moving ingress, authentication, source acquisition, or publication
business rules into that core. All webhook, repository, diff, and provider
input is untrusted until it passes the relevant verification and validation
boundary.

The first release is intentionally a public/installable GitHub App for
pull-request review automation. It does not include Marketplace listing, paid
plans, billing, OAuth user authorization, Check Runs, issue comments, or direct
unreviewed repository writes. Optional learning pull requests are a separately
accepted capability and remain review-mediated.

## Decision drivers

- least-privilege GitHub permissions and verified ingress;
- durable asynchronous processing with duplicate and stale-work protection;
- independence between the provider-neutral review engine and GitHub/provider
  transports;
- tenant and installation isolation at every durable lookup;
- credential-free local fakes and deterministic contract tests;
- a small initial operational surface with bounded failure behavior; and
- migration-safe ports, records, and rollback seams for future scaling.

## Decision

### One release-unit modular service

Start with one versioned modular service and one release unit. Stateless
replicas accept HTTPS webhooks and run bounded internal worker loops. Managed
Postgres is the durable delivery ledger, transactional job queue/outbox,
lease store, installation/repository lifecycle store, review state store, and
publication receipt store. GitHub ingress, installation authentication,
trusted-source access, publisher transport, and provider transport are adapters
behind stable application ports. The application orchestrates those ports, but
the existing `ReviewService` remains provider-neutral.

This topology gives one atomic deployment and rollback while preserving a later
split into independently deployed ingress and worker/publisher services. The
ports and versioned job records are the compatibility boundary for that split;
the initial service does not introduce Redis or a second broker.

### Ports and dependency direction

The public ports are defined in the hosted architecture contract and have the
following owner and seam expectations:

| Port | Input -> output | Owning layer | Failure contract | Local seam |
| --- | --- | --- | --- | --- |
| `WebhookVerifier.verify` | `(headers, raw_body) -> VerifiedDelivery` | GitHub ingress adapter | constant-time HMAC and shape validation; reject without persistence | deterministic HMAC fake |
| `DeliveryStore.accept` | `(delivery) -> AcceptResult` | application/persistence | atomic tenant-scoped dedupe; duplicate returns prior acceptance | in-memory delivery ledger |
| `JobQueue.enqueue/claim/renew/reschedule/complete` | delivery/job commands -> `Job`/transition result | application/persistence | compare-and-set lease/state transitions; retryable failures remain durable | in-memory queue with lease clock |
| `InstallationCredentialSource.issue` | `(scope) -> InstallationCredential` | GitHub authentication adapter | mint least-scoped short-lived credential; never persist it | expiring credential fake |
| `TrustedSourceGateway.acquire` | `(source_ref) -> TrustedReviewSource` | GitHub source adapter | pin installation/repository/PR/base/head; never execute head code | fixture source fake |
| `TenantConfigRepository.get` | `(installation_id, repository_id) -> TenantConfig` | application/persistence | tenant-scoped lookup; missing/disabled configuration is explicit | map-backed config fake |
| `ReviewEngine.review` | `(request) -> ReviewResult` | provider-neutral application adapter | existing validation boundary is fail-closed | fake `ReviewService`/provider |
| `ReviewPublisher.publish` | `(request) -> PublicationReceipt` | GitHub publisher adapter | accepts only validated results, pins reviewed `commit_id`, and reconciles marker plus post-write head before ambiguous retry | recording publisher fake |
| `Clock`, `IdGenerator` | injected time/identity -> deterministic values | application | no wall-clock/randomness hidden in domain decisions | fixed clock and sequence IDs |

HTTP, database, GitHub, and provider adapters depend inward on these ports.
Application orchestration depends on provider-neutral domain contracts. The
review core imports no GitHub, queue, database, or provider SDK. GitHub adapters
do not live in `src/code_sensei/providers/`; future application and GitHub
adapter boundaries are `src/code_sensei/hosting/` and
`src/code_sensei/hosting/github/`. Publishers receive only validated
`ReviewResult` values.

### Four required views

#### Component view

```text
GitHub -> HTTPS ingress -> WebhookVerifier
       -> Postgres delivery/job transaction -> leased worker
       -> trusted source/config -> ReviewService -> provider
       -> validated ReviewResult -> ReviewPublisher -> pull-request review
operator/admin -> audited config, pause/drain, write kill switch, purge, rotation
```

#### Trust-boundary view

```text
public Internet/GitHub (signed, untrusted input)
       -> HTTPS/HMAC boundary
hosted tenant control plane (Postgres IDs, jobs, leases, config)
       <- audited operator controls (no raw review data)
       -> ephemeral untrusted source/model processing
external provider (credential boundary)
       -> GitHub write boundary (validated result + current-head check)
```

#### Data-flow view

```text
signed raw body -> verified envelope -> IDs and base/head SHAs
  -> ephemeral base-pinned diff/context -> validated ReviewResult
  -> encrypted short-lived result envelope -> one review publication
  -> receipt/marker reconciliation -> purge
operator controls -> versioned config/control transitions only, never payloads
```

#### Deployment view

```text
HTTPS load balancer
       -> same-version stateless service replicas
          (ingress + bounded worker loops)
       -> managed Postgres (ledger/jobs/leases/lifecycle/receipts)
       +-> managed secrets/KMS
       +-> outbound GitHub/provider APIs
       +-> metrics/log/traces sink
audited operator plane -> least-privilege control APIs, Postgres metadata, KMS
```

### Pull-request review publication only

The initial publisher creates one pull-request review containing the summary
and inline comments. It uses `Pull requests: write`, sends the reviewed head as
`commit_id`, and includes a stable hidden marker for idempotency reconciliation.
It does not create a Check Run: `Checks: write` is not requested, and a second
publication state model is deferred. A publisher kill switch can stop new
GitHub writes while allowing state reconciliation and safe draining.

### Registration, permissions, and events

The App is public/installable, has an HTTPS webhook with SSL validation, and
does not provide an OAuth user authorization or callback flow for automation.
Setup and homepage URLs are informational and do not grant authorization.
Marketplace remains a separate future product decision.

Baseline installation permissions are:

| Permission | Grant |
| --- | --- |
| Metadata | read (implicit) |
| Contents | read |
| Pull requests | read and write |

The App requests no Issues, Checks, Administration, Workflows, or organization
permission. Optional learning pull requests require a separately accepted
increase to `Contents: write`; `Pull requests: write` remains required. The
feature is disabled until the new grant is accepted and reconciled, and it
never permits direct unreviewed writes.

The webhook allowlist is exact:

- `pull_request`: `opened`, `reopened`, `synchronize`, `ready_for_review`,
  `converted_to_draft`, and `closed`;
- `installation`: `created`, `deleted`, `new_permissions_accepted`, `suspend`,
  and `unsuspend`;
- `installation_repositories`: `added` and `removed`.

All other event/action pairs are ignored after signature and shape
verification and recorded only as bounded metadata/metrics. Installation and
repository-selection lifecycle events update durable state even when they do
not enqueue review work. The handler durably accepts allowed work and returns
within the GitHub webhook response budget.

Source acquisition pins the installation ID, repository ID, pull-request
number, base SHA, and head SHA. It never executes head code. Only the base SHA
supplies trusted learnings, stages, lenses, and documents.

### Lifecycle state models

Persisted state transitions are tenant-scoped and compare-and-set versioned.
Delivery verification/rejection is explicitly transient before persistence;
accepted/ignored outcomes retain bounded metadata only. The complete
source-state tables in `docs/architecture.md` are the current-state contract;
the decision summary below cannot loosen their terminal or tenant-intent guards.

#### Installation: `active` / `permission_update_required` / `suspended` / `uninstalled`

| Trigger or guard | Next state | Durable side effect |
| --- | --- | --- |
| verified `installation.created` with baseline grant | `active` | upsert tenant/install record and permission snapshot |
| verified `installation.new_permissions_accepted` with required grant | `active` | replace permission snapshot/version; preserve repository tenant choices and resume only eligible repositories |
| verified permission drift or missing required grant | `permission_update_required` | persist required/current permission versions and stop claims |
| verified `installation.suspend` | `suspended` | stop claims; cancel/supersede eligible jobs |
| verified `installation.unsuspend` with current grant | `active` or `permission_update_required` | reconcile grant and resume only if baseline is present |
| verified `installation.deleted` | `uninstalled` | revoke eligibility, cancel work, and retain sanitized audit metadata |

#### Repository selection: `enabled` / `removed` / `disabled_by_tenant`

`disabled_by_tenant` also preserves a `selected` boolean, keeping GitHub
selection separate from tenant policy.

| Trigger or guard | Next state | Durable side effect |
| --- | --- | --- |
| verified `added` hint and authoritative GitHub selection includes repository | `enabled` or `disabled_by_tenant` | CAS `selected=true`; preserve tenant disablement |
| verified `removed` hint and authoritative GitHub selection excludes repository | `removed` or `disabled_by_tenant` | CAS `selected=false`; preserve tenant disablement and stop work |
| tenant disables repository from `enabled`/`removed` | `disabled_by_tenant` | preserve `selected`, stop claims, and retain operator intent |
| tenant re-enables repository | `enabled` if selected, otherwise `removed` | authenticated tenant action clears disablement |
| installation suspended/uninstalled | unchanged | preserve selection; separate installation guard blocks claims |

Effective eligibility is `installation == active AND repository == enabled`;
GitHub lifecycle events may update `selected`, but only an authenticated tenant
action can leave `disabled_by_tenant`.
These deliveries are reconciliation hints: current GitHub selection plus a
compare-and-set `selection_version` prevents a delayed hint from reversing a
newer selection.

#### Delivery: transient `verified` / transient `rejected` / lookup `duplicate` / durable `ignored` / durable `accepted`

| Trigger or guard | Next state | Durable side effect |
| --- | --- | --- |
| HMAC/shape check succeeds before persistence | `verified` (transient) | hold only a bounded verified envelope in memory |
| signature, shape, or required identity fails | `rejected` (transient) | increment sanitized metric; never persist payload or enqueue |
| App ID + `X-GitHub-Delivery` already exists | `duplicate` | return the original acceptance and do not enqueue again |
| verified allowed lifecycle event | `accepted` | atomically write digest/identifiers and enqueue or lifecycle job |
| verified unknown action | `ignored` | record bounded identifiers/digest and reason; do not enqueue |

#### Job: `queued` / `leased` / `retry_scheduled` / `succeeded` /
`failed_terminal` / `superseded` / `cancelled`

| Trigger or guard | Next state | Durable side effect |
| --- | --- | --- |
| accepted delivery creates work | `queued` | insert tenant-scoped job with capability/schema version |
| worker compare-and-set with unexpired eligibility | `leased` | record lease owner and expiry |
| transient failure with attempts remaining | `retry_scheduled` | write next-at, class, bounded backoff metadata |
| due `retry_scheduled` job remains eligible | `queued` | clear next-at and make the job claimable |
| `leased` job expires without completion | `queued` | clear owner, increment reclaim count, and make claimable if eligible |
| successful completion | `succeeded` | persist completion time and review/publication linkage |
| retry budget exhausted or non-retryable failure | `failed_terminal` | write sanitized dead-letter/operator metadata |
| newer head or lifecycle guard wins | `superseded` | prevent downstream publication and record superseding identity |
| removal, suspension, draft/closed cancellation guard | `cancelled` | release lease and record cancellation reason |

#### Review: `pending` / `source_acquired` / `running` / `validated` /
`invalid_terminal` / `superseded`

| Trigger or guard | Next state | Durable side effect |
| --- | --- | --- |
| job accepted for review | `pending` | persist pinned install/repository/PR/base/head/config identity |
| trusted base/head source acquired | `source_acquired` | record source digest and discard source after use |
| `source_acquired` and current-head/lifecycle recheck succeeds | `running` | increment attempt metadata and lease heartbeat |
| transient provider failure with budget | `pending` | discard ephemeral data and link the job retry schedule |
| `ReviewService` returns validated `ReviewResult` | `validated` | encrypt result envelope and link publication request |
| malformed/unsafe provider result | `invalid_terminal` | persist sanitized validation error; never invoke publisher |
| newer head or lifecycle guard wins | `superseded` | mark envelope/publication ineligible and release work |

#### Publication: `pending` / `publishing` / `published` /
`published_superseded` / `retry_scheduled` / `failed_terminal` / `skipped_stale`

| Trigger or guard | Next state | Durable side effect |
| --- | --- | --- |
| validated review is current | `pending` | persist result digest and stable hidden marker |
| compare-and-set claim with current head and marker definitively absent | `publishing` | record lease/attempt and send reviewed head as `commit_id` |
| scoped marker already exists | `published` or `published_superseded` | restore receipt without a POST and reconcile current head |
| receipt/marker confirmed and head still current | `published` | store receipt, result digest, and publish time; purge envelope |
| receipt/marker confirmed but head changed after preflight | `published_superseded` | retain receipt, purge envelope, mark superseded, and enqueue eligible current head |
| transient/ambiguous response and attempts remain | `retry_scheduled` | persist bounded backoff and reconcile marker before retry |
| publisher lease expires or worker crashes with unknown outcome | `retry_scheduled` | clear owner and require scoped marker reconciliation before any POST |
| due retry and marker definitively absent | `pending` | recheck eligibility and head before another claim |
| non-retryable or exhausted publication | `failed_terminal` | store sanitized operator metadata and retain no raw body |
| head/install/repository no longer current before POST | `skipped_stale` | record current identity and perform no GitHub write |

### Durability and idempotency

Postgres records are versioned without prescribing SQL types:

- `installations` and `repositories` carry tenant key, granted-permission
  snapshot and version, lifecycle state, and created/updated timestamps;
- `deliveries` are unique by App ID plus `X-GitHub-Delivery`; they store event,
  action, installation/repository IDs, payload digest, and accepted timestamp,
  never the raw body;
- `review_jobs` are unique by installation/repository/pull-request/head SHA and
  review-configuration digest; leases use owner plus expiry and compare-and-set
  state transitions;
- `reviews` store state, base/head SHA, provider/config digests, attempt
  metadata, and an encrypted validated-result envelope retained for at most
  seven days;
- `publications` are unique by review ID plus surface and result digest. After
  every first write and ambiguous response, the publisher looks up the stable
  hidden marker before any POST. Matches require the App actor, tenant, repository, pull
  request, pinned `commit_id`, and result digest. Authentication, timeout,
  truncation, or API errors are indeterminate and reschedule without a POST.

Acceptance of a verified delivery and creation of its job are one durable
transaction. Tenant identity participates in every uniqueness key and lookup.

### Failure, retry, stale work, and cancellation

- Invalid signature or shape is rejected without enqueueing. An unknown action
  is ignored after verification and recorded only as bounded metadata/metrics.
- A duplicate delivery returns its prior acceptance. A new head marks earlier
  queued/running work superseded. Publication rechecks eligibility before POST
  and pins `commit_id`; receipt/marker reconciliation rechecks afterward. A
  raced write to the prior commit becomes `published_superseded` and queues the
  eligible current head rather than claiming that no stale write occurred.
- Installation suspend/uninstall, repository removal, and draft/closed pull
  requests stop new claims and cancel or supersede eligible work.
- On GitHub `401`, discard the credential and mint once. A `403` permission
  failure moves the installation to `permission_update_required` and blocks
  claims until the grant is reconciled. A `404`
  reconciles installation/repository state. `409`/`422` are classified as
  stale/conflict and reconciled. `429`, `5xx`, and timeouts use bounded
  exponential backoff with jitter and honor `Retry-After`.
- Provider transient failures retry within a bounded budget. Invalid
  structured output remains fail-closed and can never reach the publisher.
- A crash after a GitHub write performs marker lookup before any retry.
- Exhausted work enters a terminal/dead-letter state with sanitized operator
  metadata only.

### Secrets, privacy, tenancy, and operations

- The App private key, webhook secret, model-provider credentials, database
  credentials, and result-envelope key live in a managed secrets/KMS boundary.
  Rotation happens without code or image rebuild; secrets never enter the
  repository or logs.
- App JWTs are short-lived. Each job mints a repository/permission-scoped
  installation token that expires after one hour and remains memory-only.
- Raw webhook bodies, repository source, diffs, prompts, and raw provider
  responses are ephemeral and never written to normal logs, traces, the
  database, or object storage.
- The provider receives only the unified pull-request diff; active learnings
  and configured lens documents from the pinned base SHA (including configured
  paths and digests); optional repository name, pull-request number, title,
  and reviewer instructions; and the selected model/options. It receives no
  raw webhook, GitHub credential, unrelated source, delivery/job identifier,
  or installation metadata.
- The validated result envelope is encrypted and tenant-bound, then purged
  after publication or seven days, whichever comes first. Delivery, job,
  review, publication-receipt, and sanitized error metadata is purged within
  30 days of its terminal state. Installation/repository lifecycle metadata is
  purged within 30 days of uninstall/removal. A daily tenant-scoped purge job
  enforces these maxima; operators may configure shorter periods.
- On `installation.deleted`, eligibility and credentials are revoked
  immediately; tenant configuration and validated-result envelopes are deleted
  within 24 hours; remaining sanitized lifecycle/control metadata follows the
  30-day uninstall maximum.
- Every key and query includes App installation and repository identity;
  repository APIs cannot perform cross-tenant reads.
- Logs, metrics, and traces contain delivery/job/review/publication IDs, states,
  durations, counts, error classes, provider/model names, and digests only—no
  code or comment bodies.

### Operations contract

Operators monitor delivery verification/rejection and duplicate rates, queue
depth and claim latency, lease expiry/reclaims, retry age and classes, provider
latency/errors, stale work, marker reconciliation, terminal/dead-letter counts,
database connection pressure, and publisher kill-switch state. Alerts and
runbooks use sanitized IDs and digests only. Pause/drain, credential rotation,
retention purge, permission reconciliation, and suspend/uninstall exercises are
required before enabling a hosted runtime.

### Contract tests, migration, rollout, and rollback

The local harness uses in-memory fakes, a fixed `Clock`, deterministic IDs, a
fake `ReviewService`/provider, and a recording publisher. Without GitHub or
provider credentials it covers the happy path, duplicate delivery, invalid
HMAC, unknown action, stale head, permission removal, lease expiry,
provider-invalid output, transient retry, crash-after-publish reconciliation,
and tenant isolation. It asserts that invalid output never invokes the
publisher.

Database changes use expand/migrate/contract phases. Job schema and capability
versions are explicit; old workers can read the expanded representation until
the migration is complete. Pause/drain controls, a previous-image rollback,
and a publisher kill switch are required operational controls. No migration
may require old and new binaries to disagree about a state transition.

The first rollout is documentation and workflow setup only; it registers no
runtime App and deploys no hosted service. A future runtime rollout uses the
same-version stateless replicas, expands schema first, migrates/reconciles
records, then contracts after old workers drain. Rollback pauses claims and
publishing, drains or safely expires leases, restores the previous image, and
reopens only compatible states. Reverting the documentation/setup change is
also safe because no implementation adopts this ADR automatically.

Measurable triggers for splitting ingress from worker/publisher are sustained
claim latency or queue depth, database connection pressure, materially
different ingress/worker scaling needs, or a required blast-radius boundary.
The split must preserve every documented port and the versioned job schema.

## Alternatives considered

### Independently deployed ingress and worker/publisher services

Rejected initially because multiple deployables add cross-service
authentication, contract/version skew, distributed tracing, and coordinated
rollback before load justifies the operational cost. The durable schema and
stable ports preserve this as a later migration.

### GitHub Actions-only hosting

Rejected because tenant-owned secrets, durable cross-run state, delivery
deduplication, leases, and publisher reconciliation are weak or drift across
repositories. Per-repository workflow adoption would also make lifecycle and
permission behavior inconsistent.

### Pull-request review plus Check Run

Rejected initially because Check Runs require `Checks: write` and introduce a
second publication and reconciliation state model. A future decision can add a
surface after the single-review contract is operationally proven.

## Official GitHub references

The registration contract is based on the official documentation reviewed for
this decision:

- [Choosing permissions for a GitHub App](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/choosing-permissions-for-a-github-app)
- [Generating an installation access token](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app)
- [Validating webhook deliveries](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries)
- [Webhook best practices](https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks)
- [Webhook events and payloads](https://docs.github.com/en/webhooks/webhook-events-and-payloads)
- [Create a review for a pull request](https://docs.github.com/en/rest/pulls/reviews#create-a-review-for-a-pull-request)

## Scope

This decision governs the future hosted GitHub App's registration, ingress,
durable orchestration, trusted-source acquisition, provider handoff,
pull-request review publication, privacy, operations, rollout, and rollback
contracts. It does not implement or register the App, deploy infrastructure,
add billing/Marketplace/OAuth user authorization, or enable learning PRs.

## Validation

The documentation change is validated by the strict project-context doctor,
the repository's unit tests and compile check, deterministic artifact and
negative-path/rollback inspection, confirmation against current official
GitHub documentation, and independent Standards, Spec, and adversarial review.
Future implementation must run the credential-free contract suite described
above before any hosted registration or write is enabled.

## Follow-up work

Separate implementation work will define the hosting port types, Postgres
schema/migrations, ingress and GitHub adapters, local fakes and contract tests,
deployment/runbooks, and an App-registration checklist. Registration,
deployment, permission escalation, and learning-PR enablement each remain
separate explicitly authorized changes.

## Consequences

The service has one atomic deployment and a small initial operational surface,
but ingress and worker capacity are coupled until a split trigger is reached.
Postgres pressure, lease health, and marker reconciliation must be observed.
The design excludes direct writes and broad permissions, keeps source/provider
data ephemeral, and retains a documented split path without changing
`ReviewService`.
