# Plan: GitHub-only operation entry for PR 271

Status: Accepted (decisions D-A, D-B, D-C, D-D recorded)
Date: 2026-10-10
Branch: `feat/pr253-gap-closure` ([PR 271](https://github.com/malsabbagh/review-sensei/pull/271))
Related: [#267](https://github.com/malsabbagh/review-sensei/issues/267), [#268](https://github.com/malsabbagh/review-sensei/issues/268), ADR 0074, ADR 0076, ADR 0077

## Goal

Full review, reply, reassessment and verify go through one entry,
`run_review_trigger`. Its record lives on the pull request as App-authored
GitHub comments. The Cloudflare worker only verifies GitHub identity and issues
credentials. It has no SQLite, no Durable Objects and no stored state of any
kind. Every piece of state it holds today moves to GitHub or becomes a signed,
self-checking value (Phase 4b). The unused original-attempt journal is
removed. The limits stay 49,152 prompt bytes, 60 ordinary
dispatches, 64 total dispatches and a 60-second control deadline.

## Where PR 271 is today

Done and tested:

- `OperationHost`, `run_review_trigger` and `GitHubCommentOperationStore` in
  `src/review_sensei/hosting/github/operation_host.py`. A new process reloads
  the record and does not call the provider again
  (`tests/test_github_operation_store.py`).
- Command grammar for the media sentence and `RS-` overrides in the parser,
  trigger prefilter, workflow callers and broker recognizer.
- Every published review and reply states the maintainer commands.

Not done:

| Gap | Effect today |
| --- | --- |
| `GitHubApplication` and the CLI never call `run_review_trigger`. | Real pull requests still use the old separate paths. |
| The store loads any comment with the marker. It does not check the author. | A user could plant a forged record. Must be fixed before wiring. |
| There is no real GitHub port. There is only a protocol and an in-memory test double. | Nothing can write the record on GitHub. |
| One comment holds every event for the pull request. | 256 events per scope cannot fit GitHub's 65,536-byte comment limit. |
| Each host transaction is one write. | One trigger may make about seven writes plus reads. That cost is not counted against 60/64. |
| `github command` does not pass finding fingerprints, permission or the review result. | `@reviewsensei override RS-…` refuses with "finding id needs the current review". The media sentence returns "requires the current review result". |
| `verify` is still a no-op command. | The four-trigger entry has no live verify. |
| The journal and authorizer TypeScript modules and tests remain. | Dead SQLite code contradicts the decision. |
| ADR 0076 still describes the "SQL prepays" order and G0 in terms of an "authoritative SQL transaction". | The ADR contradicts itself. |
| The `github.operation_entry` switch is named here but does not exist in `configuration.py` or any schema. | There is no rollback lever. |
| Dispositions are honored only when a review is published (`convergence.py`, `disposition_honors_fingerprint` with `head_sha`). Verify does not load them, and the response instructions do not say to comment verify after an override. | An override is stored but changes nothing until some later review. |
| Result-part comments (D-A) have no author fence, size bound or readback rule. | A forged result part could be published as the accepted review. |
| The command job will read the review result and finding ids from GitHub. `setup.py`, `setup-content.ts` and the example workflow do not grant or pass that. | Installed repositories cannot run verify, media approval or `RS-` overrides. |
| No step removes the legacy paths. | With the route on by default, two paths stay indefinitely. |
| The worker still has two SQLite Durable Objects. `BrokerLedger` holds token replay ids, rate counters, session enrollment witnesses and one-use session grants. `DeliveryLedger` holds webhook delivery ids, setup continuation cursors (repository slugs and the permission map) and setup failure records (repository name in clear). | State, including repository names, is stored outside GitHub. |

## Decisions

**D-A. Where the accepted full-review result lives across a crash.** Decided:
App-authored part comments. The provider step and the publish step are
separate workflow steps, and `review.json` is runner-local. So the validated
review result, not prompts or model transcripts, is stored as App-authored
part comments on the same pull request. These reuse the session ledger's
bounded part pattern. The result is published on that pull request anyway, so
nothing leaves the repository. A rerun after a crash publishes from those
parts and makes no second provider call.

**D-B. Default for the new route.** The default stays `enabled`. Phase 6
measured the P1 profile and it does not fit 60 ordinary dispatches. That
measurement does not turn the route off. `disabled` remains the rollback.
The caps are never raised. A trigger that would exceed the budget fails closed
as `unavailable`. #267 stays open. Phase 7 does not run.

**D-C. What verify does.** Decided: verify is the existing-inventory
reassessment contract on the current head. It makes at most one provider
request, then runs the approval finalizer. When mixed media coverage is the
only blocker, it posts the explicit mixed APPROVE. A verify with no pending
findings makes zero provider calls and only finalizes.

## Phase 0: make the GitHub store safe (blocks everything else)

0.1 **Author fence.** Load only issue comments whose `user.id` equals the App's
numeric user id and whose `user.type` is `Bot`. Two App-authored records for
the same key are refused as ambiguous. A marker in any other author's comment
is ignored and logged as a diagnostic.

0.2 **Real port.** Add `GitHubIssueCommentOperationPort` beside
`session_ledger.py`, using `GitHubHttp`:

- `list`: issue comments, at most 10 pages of 100, require EOF
- `create`: POST, then GET by id
- `update`: PATCH, then GET by id

Readback must match the canonical payload byte for byte. A lost POST response
is reconciled by marker on the next load. It is never blindly retried.

0.3 **Split the record.** Use one index comment per pull request and one
comment per event:

- The index holds the scope root cursor and the event keys.
- Each event comment holds its event, transitions and accepted packet.

Loading still costs one listing, because listings return bodies. A write
touches only the event comment, plus the index when the root moves. Keep the
existing caps: 256 events per scope, 64 transitions per event, 4 obligations.
Refuse before work when a comment would exceed 60,000 bytes.

0.4 **Coalesce writes.** Each host call commits at most once:

- begin and the trigger note commit together
- consume commits once
- charge and accepted packet commit as two writes, because the charge must
  persist before the provider call

Add a request counter to the port. Test the physical GET/POST/PATCH count per
trigger.

0.5 **Payload allow-list.** Encode only digests, integers, booleans, the
trigger name and `AcceptedPacket` fields. Under D-A, result bytes live in
separate result-part comments, never in the operation record. Add a test that
a prompt or provider body never appears in any operation comment.

0.6 **Result-part comments (D-A).** Use their own marker,
`<!-- review-sensei-result -->`, with the same rules as the operation record:

- Load only comments written by the App (`user.id` and `user.type` `Bot`).
  Ignore any other author.
- Each part carries the operation event key, its index, the part count and
  the sha256 of the whole result. Refuse a missing, duplicate or extra part.
- Each part stays under 60,000 bytes. Refuse before the provider call when the
  bounded result would need more parts than the policy cap.
- Create, then GET by id and compare byte for byte.
- On restore, join the parts and require that the sha256 equals the accepted
  packet's `payload_digest` before anything is published.
- The allow-list test from 0.5 also runs on result parts. They hold the
  validated result only, never prompts or provider bodies.

Exit: authorship, duplicate, oversize, lost-response and readback-mismatch
tests pass for operation and result-part comments. The per-trigger request
count is recorded.

## Phase 1: admission proof from GitHub-hosted auth

Build `AdmissionProof` from what the broker issues. After Phase 4b, the
session grant is a signed value and the broker stores nothing.

| Field | Source |
| --- | --- |
| `scope_digest` | sha256 of `repository_id:pull_request` |
| `authority_digest` | the broker session attestation digest |
| `execution_identity` | sha256 of the run id and run attempt, first 32 hex digits |
| `owner_digest` | sha256 of the App id |
| `reservation_digest` | the session reservation id from the session ledger |

`server_authenticated` is true only when the broker verified the session
grant's signature, scope, attestation, audience and expiry. One-use is
enforced on GitHub, not in the worker. `consume_grant` records the grant's
sha256 in the operation record for that pull request. A digest already in the
record refuses as `replay`. Grants are bound to one `repository:pull
request:head` scope, and every writer runs in that pull request's concurrency
group (Phase 3), so the record serializes consumption. A grant cannot be
replayed into a second allowance.

`OperationRequest.event_id` is the stable GitHub event:

- the delivery id for `pull_request`
- the source comment id plus `updated_at` for mentions and commands

Exit: forged, replayed and missing attestations refuse before any operation
comment write.

## Phase 2: wire the four triggers

Routing is in `GitHubApplication`. CLI changes only pass inputs.

2.0 **Policy switch.** Add `github.operation_entry: enabled|disabled` to
`configuration.py`. Accept it in the `github` root field, and add it to the
policy schema and the generated `.reviewsensei.yml` defaults in `setup.py` and
`setup-content.ts`. The default is `enabled`. An unknown value refuses at load.
It is read from the trusted base-branch policy, never from the pull request
head. `disabled` routes all four triggers to the legacy paths. Document it in
`docs/public-contracts.md`. Phase 6 replaced the unmeasured sentence: the
profile does not fit 60 ordinary dispatches, so the default is `disabled`.

2.1 **Full review.** In `review-sensei review --transaction --github-session-ledger`:

1. begin, then consume, then `execute_and_accept`. The provider call happens
   inside this step.
2. Under D-A, write the result parts.
3. In `github review`: restore, require that the accepted packet's
   `payload_digest` equals sha256 of the result being published, then publish.
   `publish_or_reconcile` uses the review publication as the remote.
4. On a rerun, restore an existing accepted packet and skip the provider call.

2.2 **Reply.** In `generate_and_publish_reply`, the provider reply call and
validation run in `execute_and_accept`. Reply publication is the remote.

2.3 **Reassessment.** The human-assessment branch of
`generate_and_publish_reply` uses trigger `reassessment` with contract
`existing-inventory-reassessment`. `reassess()` is the provider call.

2.4 **Verify.** Per D-C, `apply_maintainer_command` routes `verify` to
`run_review_trigger(trigger="verify")`. It replaces the current
"requires an evidence-backed review result" no-op.

Verify loads `session_dispositions` from the session ledger and passes them as
`authorized_dispositions` to the reassessment and the finalizer. This is the
same input `application.py` already gives to publication. An override applies
only to the head it was recorded on. After a new push, verify reports the
override as stale and the finding blocks again. Tests:

- Override then verify: the overridden finding no longer blocks, the AI
  result stays partial, and no extra provider call is made when nothing else
  is pending.
- Override, then push, then verify: the finding blocks again and the reply
  names the stale override.

2.5 **Media sentence.** `approve-media` goes through verify. Those are two
invocations of the same source comment, as measured: confirm, then finalize
with `publish_mixed_approval`. The AI result stays partial. Requires
`maintain` or `admin`.

2.6 **Override by `RS-` id.** In `github command`:

- Read the latest App review's v2 finding markers to get current fingerprints
  and pass `finding_fingerprints`.
- Read `/collaborators/{actor}/permission` and pass `actor_permission`.
- Keep the hosted path's broker-attested actor.

2.7 **Response instructions.** Update `MAINTAINER_RESPONSE_INSTRUCTIONS` in
`presentation.py` so every review and reply also says:

- After an override, comment `@reviewsensei verify` to apply it.
- An override applies to the current commit only. A new push needs a new
  override.

`append_response_instructions` must still skip the block when it is already
present. Update the publication and conversation tests, plus
`docs/public-contracts.md`, `docs/human-assessment.md` and `README.md`.

Exit: an end-to-end synthetic GitHub test for each trigger. A kill after
acceptance makes provider calls 1 then 1 on rerun. The live `RS-` override and
media sentence pass through the CLI.

## Phase 3: workflow

- Every job that writes the operation record must be in the existing per-PR
  job concurrency group
  `reviewsensei-provider-review-<repository>-<pull request>`. That covers
  review, reply and command. Add a test that fails if a writer job leaves the
  group.
- The command job needs the review result and request comment id inputs for
  verify and media. It reads them from GitHub and takes no caller-supplied
  file.
- Do not change the frozen caller fixtures. The grammar change on PR 271
  already covers the live templates.
- Update all four live copies of the caller workflow:
  `.github/workflows/review-sensei-review.yml`,
  `examples/github-actions/review-sensei-review.yml`, the live template in
  `setup.py` and the live template in `setup-content.ts`. The workflow
  `GITHUB_TOKEN` stays read-only (`contents`, `pull-requests` and `issues`
  read). Operation and result-part comments are written with the broker's
  existing `review_session` capability (`pull_requests: write`), so the broker
  gains no new permission. The command job requests that capability and
  `review_status`, and passes no caller-supplied result file. The collaborator
  permission read uses the App's existing metadata access.
  `tests/test_entry_point_parity.py` and
  `deploy/cloudflare/test/setup-content.test.ts` must pass for both setup
  copies.
- Raise `SETUP_VERSION` from 5 to 6 in both `setup.py` and `setup-content.ts`
  so installed repositories get an update pull request. The frozen version 4
  and version 5 fixtures stay unchanged.

## Phase 4: remove the journal

- Delete `deploy/cloudflare/src/original-attempt-journal.ts`,
  `original-attempt-authorizer.ts` and their two test files. Neither is a
  Durable Object class or Worker route, so no Wrangler migration is needed.
- Set ADR 0074 to "Superseded by ADR 0076 (GitHub operation record)". Update
  `docs/adr/README.md` and `docs/epic238-integration-status.md`. Leave
  historical proposals unchanged.
- Python's "original attempt witness" checks in `session.py`,
  `assessment_queue.py` and `bounded_evidence.py` accept the operation-record
  handle as the witness. They must not be loosened to accept a snapshot.
- Retitle #268 to the GitHub record, or close it as superseded.

## Phase 4b: remove all worker SQLite and Durable Objects

After this phase `deploy/cloudflare` has no `ctx.storage`, no `sql.exec`, no
`DurableObject` class and no `durable_objects` binding. Each stored item is
replaced as follows.

| Stored today | Replacement | What changes |
| --- | --- | --- |
| One-use session grants (`broker_session_grants`) | A signed grant: HMAC-SHA256 over the scope, attestation digest, audience, run id hash and expiry, 10-minute TTL. `/github/session-grant` checks the signature and expiry. One-use is recorded on GitHub (Phase 1, `consume_grant`). | None for callers. The grant string format changes, so the broker and Python client ship together. |
| Session enrollment witness (`broker_session_enrollments`) | GitHub. `known` means an App-authored submitted pull request review with `commit_id` equal to the head, or an App-authored operation record for that pull request, already exists. Users cannot delete submitted reviews. Otherwise `enrolled`. | A crash after enrollment and before the first review or record leaves no witness. A deleted ledger comment in that window starts fresh instead of `recovery-required`. Only write-access users can delete App comments. |
| OIDC replay ids (`broker_replays`) | No stored replay set. Keep exact claim checks (repository, workflow ref and sha, event, run id, audience). Add a short maximum token age: refuse an `iat` older than 5 minutes. The minted token stays scoped to one repository and one capability. | A copied OIDC token can be exchanged again within 5 minutes for the same repository and capability. The OIDC token is a job credential, and that job can already request that token. |
| Rate counters (`broker_rates`) | None in the worker (D-D). A Cloudflare WAF rule set up outside the repository. | Worker requests are no longer counted per scope. |
| Webhook delivery ids (`deliveries`) | No dedup store. Setup is idempotent on GitHub: branch creation and pull request creation return 422 when they already exist, and `github-app.ts` already treats that as done. GitHub does not redeliver automatically. A manual redelivery reruns the same idempotent setup. | Two concurrent deliveries for the same repository both run. One gets 422 and stops. Add a test for that race. |
| Setup continuation cursors and alarm (`setup_continuations`) | A signed cursor. After each repository, the worker calls its own `/github/setup-continue` route through a self service binding (`services` in `wrangler.jsonc`), not the public URL, inside `ctx.waitUntil`. The request carries an HMAC-signed cursor: installation id, delivery id, page, offset and expiry. The repository list and permissions are re-read from GitHub with the installation token on each step. The cursor never holds repository names or the permission map. | No durable alarm. If a continuation request is lost, setup stops part way. Recovery is redelivering the webhook from the App settings, or the next `installation_repositories` event. Setup is idempotent, so a rerun is safe. |
| Setup failure records (`setup_outcomes`) | Workers logs only, with `delivery_id`, `error_code` and failure count. No repository name or slug in any log line. | No queryable failure table. |

Work:

- Add one Worker secret, `REVIEWSENSEI_SIGNING_KEY`. Derive separate keys
  with HKDF labels `session-grant-v1` and `setup-cursor-v1`, so a grant cannot
  verify as a cursor. Missing secret refuses with
  `configuration_unavailable`.
- Delete `broker-ledger.ts`, `delivery-ledger.ts` and `setup-alarm.ts`, plus
  `broker-ledger.test.ts` and `delivery-ledger.test.ts`. Rewrite
  `claimLedger`, `enrollSession` and `sessionGrantLedger` in
  `token-broker.ts`, and `ledgerRequest` in `worker.ts`.
- Remove `DELIVERY_LEDGER` and `BROKER_LEDGER` from `env.ts` and
  `wrangler.jsonc`. Add migration tag `v3` with
  `"deleted_classes": ["DeliveryLedger", "BrokerLedger"]`. Deploying it
  deletes the stored rows. They are only replay, rate and dedup state, so
  nothing needs exporting.
- Update `worker-webhook.test.ts`, `token-broker.test.ts`,
  `epic238-consuming-broker-fixture.ts` and `epic238-consuming-grants.test.ts`
  for signed grants and stateless webhooks. Python `broker_client.py` keeps
  accepting only `enrolled` and `known`.
- The legacy session-ledger write path, which stays while
  `github.operation_entry` exists, also records the consumed grant digest in
  the App-authored session ledger comment and refuses a repeat. Without this,
  a disabled switch would make grants reusable for 10 minutes.
- Add a test that fails if any file under `deploy/cloudflare/src` uses
  `ctx.storage`, `sql.exec`, `DurableObject` or `setAlarm`, or if
  `wrangler.jsonc` declares `durable_objects`, `kv_namespaces`,
  `d1_databases` or `r2_buckets`.
- Update `deploy/cloudflare/README.md`, `docs/data-handling.md` and the ADRs
  that name `BrokerLedger` or `DeliveryLedger`. The worker stores nothing.

**D-D. Rate limiting without SQLite.** Decided: no rate limit in the worker.
Delete `admitScope`, the `preauth` admit call and the `broker_rate_limited`
error. Python `broker_client.py` drops its `rate_limited` branch. Abuse
protection is GitHub's own limits on the App installation, plus a Cloudflare
WAF rate-limiting rule on `/github/token` and `/github/session-grant`. That
rule is configured in the Cloudflare dashboard, outside this repository.
`deploy/cloudflare/README.md` documents the rule as a deploy step. The worker
does not depend on it to be correct. Do not add the `ratelimits` binding.

## Phase 5: ADR 0076 corrections

- Replace "Order: SQL prepays…" with the GitHub order: operation comment
  charge, then provider and validation, then accepted packet comment, then
  result parts, then publication, then acknowledgement.
- In G0, change "authoritative SQL transaction" to "authoritative operation
  comment write".
- Record D-A, D-B and D-C as decided. Remove "Broker SQL owns…".
- If `docs/architecture.md` changes, copy normative lines verbatim into
  `docs/architecture/mandatory-rules.md` and rerun
  `tests/test_pr253_context_policy.py`.

## Phase 6: measure P1 (#267)

- Run the P1 profile with synthetic HTTP that counts every physical request:
  one pull request, four pending instances, one provider request, two
  checkpoints.
- Then run one trace on a sandbox repository.
- If it does not fit 60/64 and 60 seconds, change the default to `disabled`
  and reopen #267 with the trace. Do not raise the caps.
- If it fits, close #267 and remove the "not yet measured" sentence from 2.0.

Outcome: the synthetic trace does not fit. Two GitHub checkpoint activations
cost 44 dispatches (38 GitHub and 6 broker). Operation-comment acceptance then
uses the remaining ordinary dispatches and stops at 60, before the result-part
readback. Four fence slots remain and cannot pay for that ordinary GET. The
provider ran once. Accepted readback and replay did not run. Elapsed synthetic
time was under a second; the dispatch count is the failure. No sandbox trace
was run. The product decision keeps the default `enabled`. `disabled` remains
the rollback. Caps stay 60 ordinary, 64 total, and 60 seconds. #267 stays
open. Phase 7 does not run.

## Phase 7: remove the legacy paths (after Phase 6 passes)

- Delete the old separate review, reply, reassessment and verify routes in
  `GitHubApplication` and the CLI that bypass `run_review_trigger`.
- Remove `github.operation_entry` from `configuration.py`, the schema and the
  setup defaults. An existing `disabled` value refuses at load with "the
  operation entry is the only route; remove github.operation_entry".
- Delete the tests that only cover the legacy routes. Keep every behavior test
  by moving it onto the single entry.
- Exit: a search for the old route functions finds no callers, and the full
  pytest suite passes.

Phase 7 runs only if Phase 6 passes. Otherwise the switch stays and #267
stays open.

## Order and parallel work

1. Phase 0, including 0.6, runs first.
2. After Phase 0, four streams can run in parallel:
   - Phases 2.0, 1 and 2.1
   - Phases 2.6 and 2.7, which are independent
   - Phases 4 and 5, which are docs and deletion
3. Phases 2.2 to 2.5 follow Phase 1.
4. Phase 4b follows Phase 1. Grant one-use must already be recorded on GitHub
   before the worker stops tracking it. The webhook and setup parts of 4b do
   not depend on Phase 1 and can start after Phase 0.
5. Phase 3 follows Phase 2.
6. Phase 6 follows Phases 3 and 4b.
7. Phase 7 runs last, and only if Phase 6 passes.

Each phase is its own commit on `feat/pr253-gap-closure`.

## Validation per phase

- `ruff format` and `ruff check` on touched Python.
- `PYTHONPATH=src python3 -m pytest` on the touched tests, plus
  `tests/test_github_trigger.py`, `tests/test_pr253_context_policy.py` and the
  publication and conversation suites.
- `cd deploy/cloudflare && ./node_modules/.bin/vitest run` after Phases 3, 4
  and 4b.
- `npx tsc --noEmit` in `deploy/cloudflare` after Phases 4 and 4b.

## Out of scope

- Publishing packages, deploying the Worker, canary activation, live provider
  calls outside the Phase 6 sandbox trace, and raising any limit.
- [#269](https://github.com/malsabbagh/review-sensei/issues/269), qualifying
  joint history capacity and deployed readers. It is separate from this route
  and stays open.
- [#270](https://github.com/malsabbagh/review-sensei/issues/270), re-auditing
  the mandatory architecture rules for the 49,152-byte frame. Phase 5 only
  reruns the existing parity test when `docs/architecture.md` changes. It does
  not do the re-audit.

## Rollback

Before Phase 7, set `github.operation_entry: disabled` in the repository
policy. The legacy paths still exist, so the switch restores them. After
Phase 7, rollback is reverting the Phase 7 commit.

The switch does not bring back the worker's SQLite. After Phase 4b is
deployed, rolling it back means redeploying the previous Worker version. Its
migration recreates empty ledgers, so replay ids, rate counters and
enrollment witnesses start empty. In-flight setup continuations are dropped
at deploy. Setup for an affected installation is rerun from the start.

Operation comments stay on the pull request for audit and are never deleted.
A disabled route does not reset a recorded charge.
