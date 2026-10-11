# Plan: fit the P1 profile inside 60 ordinary requests

Status: Proposed. Tracks [#267](https://github.com/malsabbagh/review-sensei/issues/267).

## Goal

Run the P1 profile through the shared operation entry with the caps unchanged:
60 ordinary requests, 64 total, 60 seconds, 49,152 prompt bytes. The profile is
one pull request, four pending findings, one provider call, and two durable
checkpoints. It then reads back the accepted result before acknowledgement. A
replay after a crash makes no second provider call.

`github.operation_entry` stays `enabled` by default throughout. `disabled`
remains the rollback.

## Where the requests go today

Measured with `tests/test_p1_public_profile.py`, one request per physical call:

| Step | GitHub | Broker | Total |
| --- | --- | --- | --- |
| First token exchange | 0 | 1 | 1 |
| Reserve the operation | 6 | 1 | 7 |
| Grant for checkpoint 1 | 2 | 1 | 3 |
| Checkpoint 1 | 12 | 1 | 13 |
| Grant for checkpoint 2 | 3 | 1 | 4 |
| Checkpoint 2 | 15 | 1 | 16 |
| Operation record and result | 16 | 0 | 16 |
| **Stopped here at 60** | | | |
| Result read-back (needed) | 1 | 0 | 1 |
| Crash replay (needed) | 3 | 0 | 3 |

The complete profile needs about 64 ordinary requests. Across the checkpoint
steps, the 38 GitHub calls are:

- 16 reads of page 1 of the comment list (`per_page=5`). The pull request has
  only three comments, so every read returns the same page.
- 7 reads of the pull request head.
- 10 reads of stored part comments. Each checkpoint reads the new parts before
  the patch, the old parts, and the new parts again after the patch.
- 2 part posts and 3 root patches.

The operation record makes four transactions: begin, consume, charge, and
packet. Begin and consume each write both an event comment and an index
comment. Every write is followed by a read-back GET. The result then needs one
more list, one post, and one read-back. The record also starts with
`GET /user` to learn the bot's id.

The cost comes from repeating reads and splitting one logical write into
several, not from the size of each request. The checkpoint receipt is 3,683
bytes and the root comment is 2,259 bytes. A comment can hold 60,000.

### These numbers are for one small synthetic pull request

The trace runs against fake GitHub. That pull request has three issue comments
(the session root and two parts), four findings in four small files, no human
conversation, the history graph off, and one provider call. Nothing fails or
retries, and there is no network latency. It is the smallest case, so a real
pull request costs more:

- **Comment count.** Each comment-list read fetches pages of 5 until a short
  page. One read costs `ceil((comments + 1) / 5)` requests, and the count
  includes ReviewSensei's own root, parts, operation comments and replies. The
  16 list reads cost 16 at 3 comments, 48 at 10, 96 at 25, 336 at 100, and
  656 at 200. This is the largest variable. The trace declares one scan page,
  which only works up to 4 comments. The list is every comment on the
  Conversation tab: resolved or not, hidden or not, from people, bots, and
  ReviewSensei. Inline comments on diff lines are a different list and do not
  count here. At 100 comments the reservation alone needs about 84 requests,
  so the operation refuses as `unavailable` before the provider is called.
- **Findings.** More findings make a larger receipt. Above about 23,500
  compressed bytes the receipt needs more parts, and each part is read several
  times per checkpoint.
- **Provider calls.** Each extra batch adds checkpoint saves of roughly 13 to
  16 requests each. Today the budget does not fit a second call.
- **Result size.** A result over about 44 KB needs more result-part comments.
- **Failures.** A lost or ambiguous write response adds a reconcile read.

## What one request can carry instead

Each item below makes one request do the work of several. None raises a cap.

1. **A write response is its own read-back.** GitHub returns the stored comment
   in the `201` from a POST and the `200` from a PATCH. Compare that body byte for
   byte, as the separate GET does today. Keep the GET only when the response is
   lost or ambiguous (timeout, `5xx`), which is the existing reconcile path. A
   later concurrent edit is still caught by the fence scan after the patch.
2. **One comment-list page serves every reader in one step.** The page already
   returns the full bodies of the root and its parts. Read parts from that page
   instead of fetching each one by id, and reuse the page until the next write.
3. **Parts are checked once per operation.** Parts are content-addressed. A
   part whose id and digest were verified earlier in the same operation does not
   need to be fetched again.
4. **A small receipt lives inside the root comment.** When the queue receipt
   fits the root comment bound, write it inline and post no separate part.
   Large inventories keep parts. Readers still read the part-based v4 and v5
   roots, and the frozen fixtures do not change.
5. **One event comment carries the whole record.** Store consumed grant ids and
   the scope cursor in the event comment, and stop writing the index on the hot
   path. Load already rebuilds the index from event comments when it is missing.
6. **One write before the provider and one after.** Begin, trigger note,
   consume, and provider charge have no external effect between them, so they
   commit as one transaction. The charge is still durable before the provider
   call. The packet commits after the call.
7. **The accepted packet carries the result.** When the validated result fits
   the event comment, it goes in the same PATCH as the packet. Larger results
   keep result parts. Replay then reads the result from the list it already made.
8. **Identity comes from comments already read.** The bot's numeric id is the
   author of the validated session root, which uses the same App. Drop
   `GET /user`.
9. **One head read per mutation.** Read the pull request head once, directly
   before each patch, instead of also at grant issue and in the tail plan.

Two larger options are not needed to fit and stay out of the first pass:

- **Sequenced broker grant.** One exchange returns a grant that covers the
  reservation and each checkpoint by sequence number. One-use stays enforced by
  the consumed digest recorded on GitHub. The broker would drop from 6 calls to
  about 2. This changes the broker contract and needs an ADR change.
- **GraphQL batching.** One query can return the pull request head, the first
  comments with bodies, and named comment nodes. One mutation request can carry
  several writes. It adds a second transport with partial-failure semantics.

10. **The fence read asks only for what changed.** The issue-comment list
    accepts `since`, which returns only comments created or edited at or after a
    time. After the first full read, a fence read with `since` set to the
    server time of that read usually returns one short page, however many
    comments the pull request has. A competing root written later is still
    returned. Use the server's timestamp, not the runner clock, minus a small
    margin.

11. **Pages hold 100 comments unless they are too big.** The 5 per page comes
    from the worst case: five maximum-size comments fill the 512 KiB response
    bound. Most comments are small. `GitHubHttp.paginate` already supports
    falling page sizes. It asks for 100, and only when a page exceeds the
    response bound does it retry the same offset with a smaller size, such as
    20 and then 5. The budgeted reader uses that with `(100, 20, 5)` and
    charges every attempt, including an oversized one. The 512 KiB bound does
    not change. A full read of 200 ordinary comments then costs 2 or 3 requests
    instead of 41. A run of very large comments still falls back to 5 per page.
    Today `paginate` never steps back up: one oversized page makes every later
    page small. Change it to return to the larger size at the next aligned
    offset, so one cluster of large comments costs a few extra requests, not
    the rest of the list.

Fewer full reads (item 2) and `since` fence reads (item 10) do not fit a long
discussion by themselves. One full read of 200 comments at 5 per page is 41
requests. Item 11 is what keeps a 100 to 300 comment pull request inside 60.

After items 2, 10 and 11, cost follows the total size of the comments more
than their number. One full read costs about the larger of `comments / 100`
and `total comment bytes / 512 KiB`, plus a few retries around large comments.
These limits remain:

- The reader stops at 1,000 Conversation comments and refuses beyond that.
- A conversation made mostly of very large comments, such as a bot posting a
  new 50 KB report on every push, can still exceed the budget. 300 comments of
  50 KB are about 15 MB, at least 30 requests to read.
- Every trigger leaves its operation comments on the pull request, so
  ReviewSensei's own comments add to this count over time.

## Phases

Each phase is its own commit. Each one updates `tests/test_p1_public_profile.py`
to assert the exact request count it reaches, so a later change cannot quietly
add requests back.

### Phase R1: operation record (items 1, 5, 6, 7, 8)

Change `GitHubCommentOperationStore`, `GitHubIssueCommentOperationPort`,
`run_review_trigger` and `run_hosted_provider`.

- Commit begin, trigger note, consume, and charge as one transaction.
- Write no index on the hot path. Grant ids and the scope cursor move into the
  event comment. Load still reads an existing index.
- Put a result that fits into the packet PATCH.
- Use the write response as read-back.
- Take the bot id from the session root author.

Measured: the operation record and result are 5 requests, including replay.
The complete profile charges **49** and fits. Checkpoints are still 44.

Exit: the full profile in the test completes. It includes accepted read-back and
replay, makes provider calls 1 then 1, and stays at or under 60 ordinary
requests. `test_budget_and_deadline_refuse_without_refund` and the operation
store tests stay green. Records written by the current format still load.

### Phase R2: session ledger reads (items 2, 3, 9, 10, 11)

Change `GitHubIssueCommentSessionLedger` and the tail plan in `replace_with_tail`.

- Read the full comment list once per operation, with falling page sizes
  `(100, 20, 5)`. Each patch is fenced by a `since` read. Reuse what was read
  in between when no write happened. The operation record shares that one list
  instead of making its own.
- Read parts from the listed page, and cache verified parts by id and digest
  for the operation.
- Read the head once before each patch.
- The tail plan reserves only the requests it will make. Unused optional scan
  pages stay charged, as they are now.

Expected: checkpoints drop from 44 to about **24**, and the complete profile
to about **30**. On a busy pull request the gain is larger. At 200 ordinary
comments, the list reads drop from about 656 requests to about 8: one full read
of 2 or 3 pages plus about 5 `since` reads.

Measured: the two checkpoints charge **31** (25 GitHub and 6 broker). The
complete profile, including accepted read-back and replay, charges **36**.
A grant is still refused before it is installed when the head is stale, so
that head read stays. History scans do not reuse this cache: each lookup reads
the list again, and a declared page bound that cannot see the end of the list
still refuses. 25 comments, 300 ordinary comments, and 300 comments with a run
of maximum-size bot comments all finish inside 60. The oversized run falls to
pages of 5 and the following page returns to 100.

Exit: the activation tail tests stay green, including stale head, stale root,
conflicting writer, and lost patch response. The P1 test asserts the new count.
More P1 cases also fit inside 60 ordinary requests: 25 comments, 300 ordinary
comments, and 300 comments that include a run of maximum-size bot comments,
which forces the smaller pages for a stretch and then returns to 100.

### Phase R3: inline small receipts (item 4)

Change the session root writer and reader.

- Write the receipt inline when it fits. Otherwise write parts.
- Readers accept both. The v4 and v5 fixtures stay byte for byte the same.

Expected: the two part posts and the remaining part reads go away. Checkpoints
drop to about **18**, and the complete profile to about **24**.

Exit: the frozen fixture tests pass unchanged. The P1 test asserts the new
count.

### Phase R4: sandbox trace and #267

Run the same profile once on a sandbox repository with real GitHub latency, on
a pull request with ordinary human conversation, not an empty one.
Record request count and wall time on #267. If both fit, close #267 and remove
the measured-failure sentence from the docs. Phase 7 of the operation-entry
plan can then start.

## Risks

- **Wall time.** At roughly 0.2 to 0.5 seconds per GitHub request, 60 requests
  alone take 12 to 30 seconds. Fewer requests also shorten wall time. Before
  R4, confirm whether the provider call runs inside the 60-second control
  window. If it does, a slow model can miss the deadline even with few
  requests.
- **Write response as read-back.** This relies on GitHub returning the stored
  body in the write response. If a response is missing or does not match, fall
  back to the GET.
- **Merged transaction.** A failure inside the one pre-provider write must leave
  no event, the same as a failed begin does today.
- **Format change in R3.** Writers change. Readers must keep accepting every
  root format already on pull requests.

## Out of scope

Raising any cap, deploying the Worker, publishing packages, and the sequenced
broker grant and GraphQL options above. #269 and #270 stay open.
