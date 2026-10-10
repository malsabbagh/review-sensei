# Plan: GitHub-only operation entry for PR 271

Status: Proposed
Date: 2026-10-10
Branch: `feat/pr253-gap-closure` ([PR 271](https://github.com/malsabbagh/review-sensei/pull/271))
Related: [#267](https://github.com/malsabbagh/review-sensei/issues/267), [#268](https://github.com/malsabbagh/review-sensei/issues/268), ADR 0074, ADR 0076, ADR 0077

## Goal

Full review, reply, reassessment and verify go through one entry,
`run_review_trigger`. Its record lives on the pull request as App-authored
GitHub comments. The Cloudflare worker issues credentials and deduplicates
webhooks. It stores no review or operation state. The unused original-attempt
journal is removed. The limits stay 49,152 prompt bytes, 60 ordinary
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

## Decisions needed before Phase 2

**D-A. Where the accepted full-review result lives across a crash.** The
provider step and the publish step are separate workflow steps. `review.json`
is runner-local. The operation record holds only its digest. A rerun after a
crash restores the accepted packet, but it has no bytes to publish.

- Recommended: store the validated review result, not prompts or model
  transcripts, as App-authored part comments on the same pull request. Reuse
  the session ledger's bounded part pattern. The result is published on that
  pull request anyway, so nothing leaves the repository.
- Alternative: keep only the digest. A crash after acceptance stays pending
  and needs a fresh maintainer-requested review, which makes another provider
  call.

**D-B. Default for the new route before the #267 measurement.**

- Recommended: wire all four triggers behind the trusted policy switch
  `github.operation_entry: enabled|disabled`, default `disabled`. Enable it
  after the Phase 6 trace fits 60/64.
- Alternative: default enabled now and accept that the 64/60 claim is
  unmeasured.

**D-C. What verify does.** Recommended: verify is the existing-inventory
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

Exit: authorship, duplicate, oversize, lost-response and readback-mismatch
tests pass. The per-trigger request count is recorded.

## Phase 1: admission proof from GitHub-hosted auth

Build `AdmissionProof` from what the broker already issues. `BrokerLedger`
stays because it is auth state.

| Field | Source |
| --- | --- |
| `scope_digest` | sha256 of `repository_id:pull_request` |
| `authority_digest` | the broker session attestation digest |
| `execution_identity` | sha256 of the run id and run attempt, first 32 hex digits |
| `owner_digest` | sha256 of the App id |
| `reservation_digest` | the session reservation id from the session ledger |

`server_authenticated` is true only when the broker session grant verified.
`consume_grant` calls the broker's one-use session grant verification. A grant
cannot be replayed into a second allowance.

`OperationRequest.event_id` is the stable GitHub event:

- the delivery id for `pull_request`
- the source comment id plus `updated_at` for mentions and commands

Exit: forged, replayed and missing attestations refuse before any operation
comment write.

## Phase 2: wire the four triggers

Routing is in `GitHubApplication`. CLI changes only pass inputs.

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

2.5 **Media sentence.** `approve-media` goes through verify. Those are two
invocations of the same source comment, as measured: confirm, then finalize
with `publish_mixed_approval`. The AI result stays partial. Requires
`maintain` or `admin`.

2.6 **Override by `RS-` id.** In `github command`:

- Read the latest App review's v2 finding markers to get current fingerprints
  and pass `finding_fingerprints`.
- Read `/collaborators/{actor}/permission` and pass `actor_permission`.
- Keep the hosted path's broker-attested actor.

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
- Keep `BrokerLedger` (token ids, rate limits, session grants, enrollment
  witnesses) and `DeliveryLedger` (webhook delivery ids, setup continuation).
  Add a test that lists their `CREATE TABLE` columns and fails if a review,
  prompt, finding or result column appears.
- Retitle #268 to the GitHub record, or close it as superseded.

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
- Then run one trace on a sandbox repository with the policy switch on.
- If it does not fit 60/64 and 60 seconds, the switch stays `disabled`. Do not
  raise the caps.

## Order and parallel work

1. Phase 0 runs first.
2. After Phase 0, three streams can run in parallel:
   - Phases 1 and 2.1
   - Phase 2.6, which is independent
   - Phases 4 and 5, which are docs and deletion
3. Phases 2.2 to 2.5 follow Phase 1.
4. Phase 3 follows Phase 2.
5. Phase 6 runs last.

Each phase is its own commit on `feat/pr253-gap-closure`.

## Validation per phase

- `ruff format` and `ruff check` on touched Python.
- `PYTHONPATH=src python3 -m pytest` on the touched tests, plus
  `tests/test_github_trigger.py`, `tests/test_pr253_context_policy.py` and the
  publication and conversation suites.
- `cd deploy/cloudflare && ./node_modules/.bin/vitest run` after Phase 4.

## Out of scope

Publishing packages, deploying the Worker, canary activation, live provider
calls outside the Phase 6 sandbox trace, and raising any limit.

## Rollback

Set `github.operation_entry: disabled`. The legacy paths still exist until
Phase 6 passes. Operation comments stay on the pull request for audit and are
never deleted. A disabled route does not reset a recorded charge.
