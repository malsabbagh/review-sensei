# Human assessment and complete selected feedback

## Request reassessment on the current PR

A human with OWNER, MEMBER or COLLABORATOR association can post an issue
conversation comment on the PR or an inline thread reply containing a standalone
`@reviewsensei`. ASCII case variants work; the legacy `@sensei` handle remains.
For example:

```text
@reviewsensei This worker is local-only. The current patch calls
reject_remote_requests(data) before transmission. Please reassess the concern
on src/worker.py.
```

The explanation is untrusted reference data. It requests judgment supported by
the current complete patch; it grants no approval authority. The default hosted
route separately reloads the complete triggering comment, admits at most 4,096
UTF-8 bytes, and rejects changed or unauthorized sources. A reply can address
only the findings supported by validated human and current-diff citations.
The full published human inventory and every other approval blocker remain.

`@reviewsensei Please approve`, bare dismissal, unrelated explanation and manual
thread resolution do not clear that inventory. A changed head/base needs a fresh
compatible current review. If a legacy review lacks its complete inventory, run
a new full review; prose and short display IDs cannot reconstruct authority.

## Keep the workflows distinct

| Action | Meaning |
| --- | --- |
| Ordinary discussion | Selected chat context: at most twenty messages, up to 1,024 bytes per message. It is not a complete feedback archive. |
| Human reassessment | Complete admitted factual source and complete required current patches, validated against existing pending finding identities. |
| Explicit targeted reassessment | Trusted API selection of full finding IDs; scopes dispatch while preserving all other obligations. Target validation and durable operation accounting belong to the queue contract. |
| Broader discovery | A separate bounded discovery wave under trusted unified/automatic-review/write policy; omission cannot retire an earlier concern. |
| Session disposition | A maintainer decision bound to the session/finding/head; it grants no blanket human-completion or approval authority. |
| Thread resolution | Conversation state; independently maintained human obligations remain until valid reassessment. |
| Approval | A separate exact-head finalizer operation after all applicable review, coverage, qualification, capability and blocker checks. |

There is no general `mark human assessment complete` command. Recognized parser
commands are `review status`, `review pause`, `review continue`, `review reenroll`,
`verify`, `dismiss|defer|accept-risk <16–64 hex ID> --reason <reason>`,
`I reviewed the media files and I approve`, and
`override <RS-id> acceptable risk` or `<RS-id> is acceptable risk`, each
preceded by the standalone mention. The media sentence and the `RS-` override
require a maintainer or admin. The published review repeats those commands.
After an override, comment `@reviewsensei verify` to apply it. An override
applies to the current commit only. A new push needs a new override.
Disposition reasons have a 512-byte limit.
`review continue` unpauses the session; it does not replenish an assessment
operation's resource allowance. These command IDs are distinct from the complete
64-hex finding IDs required by the proposed targeted assessment API.

## Authorized operator invocation

The command is `review-sensei github reply`. Its current help supports:

```bash
review-sensei github reply --generate --config <trusted-policy> \
  --repository <owner/repo> --pull-request <number> \
  --source-comment-id <id> --source-updated-at <exact-updated-at> \
  --head-sha <current-head-sha> --source-kind issue \
  --enable-reply --allow-write
```

For inline feedback use `--source-kind inline --root-comment-id <thread-root-id>`.
These placeholders describe an authorized operator invocation, not permission to
write to a historical PR. Authentication follows the configured existing
broker/OIDC contract; this example creates no new credential. The additional
`--enable-broader-review` flag needs trusted unified mode, automatic-review policy
and writes. It does not clear old findings by omission.

## Malformed provider JSON

The legacy human reassessment service can request one fresh JSON response when
the first response has invalid JSON syntax. It reuses the complete original
reference data and never sends the malformed output back as correction context.
Both calls consume the original call, byte, token and time allowances; a lower
budget can prevent the correction. Valid JSON uses one call. Invalid schema,
unsupported citations, oversize output and incomplete provider transport or
completion are terminal failures, without a syntax correction. Every corrected
decision still passes the same schema and literal evidence checks before any
reply or eligibility refresh can be published.

Failure messages report a closed reason code and, for JSON syntax, numeric
parser line/column/offset and response bytes, with correction/budget status.
They contain no source text, output excerpts or parser message. A syntax error
alone cannot prove that output was truncated; explicit incomplete or
length-terminated completion metadata is rejected by the provider adapter.
Provider adapters must enforce their transport and token ceilings. This local
reliability change does not activate a rich feedback route, grant durable
recovery, or change approval policy.

## Complete selection API: isolated implementation, integration pending

Epic [#238](https://github.com/malsabbagh/review-sensei/issues/238), lane
[#243](https://github.com/malsabbagh/review-sensei/issues/243), introduces
`feedback-v1` domain admission and hosted read-only adapter seams. The default
mention/CLI route still selects the single trigger with the legacy 4 KiB limit.
This lane alone does not enable long or multi-comment assessment in installed or
deployed consumers. Reader-first A/B/C integration and exact-head qualification
are required before those support claims.

The trusted host supplies an ordered tuple of `FeedbackReference(kind,
comment_id, updated_at)` values to `HumanAssessmentPublisher.load_feedback`.
The trigger must appear with its exact timestamp and authorized mention. Every
participant is reloaded through a canonical same-repository PR endpoint and must
be a currently authorized human User. The snapshot retains complete original
UTF-8 body, byte length/digest, numeric author ID, login, association and inline
parent identity. It binds repository, PR, base/head, ordered sources and explicit
full target IDs. Quoted prose, links and forged markers cannot select sources or
targets. C validates targets against the exact inventory; D does not resolve
display prefixes or invent a command syntax.

Finite **domain admission** ceilings are 32 sources, 65,536 UTF-8 bytes per source
and 262,144 total raw source bytes. No normalization or clipping occurs. These
ceilings are not promises that all admitted selections fit an execution: JSON
escaping, instruction/framing, inventory and complete diff bytes count against
the existing configured provider prompt ceiling. `FeedbackSelection.render_prompt`
preflights the complete selection plus supplied framing/data; overflow refuses
with `feedback_prompt_oversized`. A citation must occur inside one original body,
never across adjacent sources or generated framing. No fragment-only assessment
can certify a complete selected explanation.

`load_feedback` and `revalidate_feedback` require the same caller-owned
`before_read: Callable[[], float]` accounting seam. It charges each physical GET
attempt before dispatch and returns a timeout no larger than 60 seconds or the
remaining absolute caller deadline. The caller shares one read counter/deadline
with A/D/E metadata, parts, source and final fences. Revalidation creates no fresh
allowance. A selection needing 32 admission reads plus two complete revalidation
passes already needs 96 reads before inventory/evidence/finalizer work; it cannot
fit a shared 64-read envelope. The caller must refuse that operation rather than
skip fences. The existing 120-second execution horizon and 60-second acquisition
horizon remain separate; operation-wide consolidation belongs to A/C integration.

Reload **every** participating source before reply publication and again
immediately before accepted receipt/eligibility activation. Changed text,
timestamp, actor, inline parent, PR association, deletion or revoked authorization
invalidates the selection; a fresh source cannot inherit old decisions. Source
digests detect changes but do not authenticate authors or give approval authority.
GitHub lacks an atomic multi-comment read: these sequential fences reduce the
race window but do not remove it. Exact current head/base/latest authority must
also be fenced by the integration owner.

Shared evidence partition persistence belongs to A, complete inventory identities
to B, and operation/target/receipt accounting to C. This lane creates no alternate
part codec or authority store. Failed admission/fencing/prompt preflight leaves
prior authority and all pending obligations intact. Report reply delivery,
assessed/resolved/unprocessed counts and finalizer results separately.

## Explicit rich preparation helpers: activation still pending

The isolated source API now supports `HumanAssessmentPublisher.prepare_feedback`
with a caller-bound `FeedbackBudgetHttp`, or `before_read` supplied to the publisher
constructor. It selects the complete authenticated trigger by default, using the
already trusted kind, numeric comment ID and exact updated timestamp. The ordinary
`prepare` route and installed CLI defaults retain the legacy 4 KiB limit. No
conversation text, URL, marker, short ID or model output opts into rich selection.

For an operator-selected set, `FeedbackSelector` carries repository, PR, exact
base/head, the triggering `FeedbackReference`, ordered source references and full
target IDs. `FeedbackSelector.from_json(bytes)` accepts a closed document with
`interface: "feedback-selector-v1"`, `repository`, `pull_request`, `base_sha`,
`head_sha`, `trigger`, `sources` and `target_ids` only. Each reference contains only
`kind`, `comment_id` and `updated_at`. The parser accepts at most 32 KiB, refuses
duplicate/unknown fields, invalid UTF-8, duplicate source identities, ambiguous
prefix targets and more than 32 sources/250 targets. Bodies and authors are
reloaded; a selector does not attest authorization. Multiple source selection
requires explicit full targets under the reviewed engineering scope. Public CLI
exposure and activation still require coordinated product qualification.

The host can call `load_selected_feedback` separately for admission or
`prepare_feedback` for current inventory/evidence preparation. Direct rich
preparation authenticates every participant again, including numeric actors and
inline root, before using the selection. `PreparedHumanAssessment.feedback` is
passed intact to the core service or queue assessment and its validators. Publishing
checks all selected sources before replying and again before recording assessment
authority; broader publication uses the same `source_fence` helper. These calls
still need the coordinator's dedicated live-source broker grants and C's exact
inventory/operation receipt binding before public activation.

`FeedbackSelection.event_key` hashes a canonical document with domain
`reviewsensei:feedback-event:v1`, repository, PR and trigger `{kind, comment_id}`.
It excludes updated timestamps, source bodies/actors, order/targets and mutable
base/head. The coordinator scopes journal lookup to the original inventory.
An edit of the same trigger finds the existing event; a changed complete operation
binding refuses rather than creating a fresh allowance. A genuinely new numeric
trigger ID is a distinct event. `FeedbackSelection.digest` remains the unchanged
`feedback-v1` complete content/snapshot binding.

`authorization_document()` returns exactly `to_dict()` metadata with body omitted
from each ordered source, plus `event_key` and `selection_digest`. `total_bytes`,
each complete body length/SHA256, numeric author/login/association, exact timestamp
and inline root remain. This metadata is not a grant. The broker must independently
reload the exact canonical issue/inline endpoints, verify current human authority
and trigger OIDC login/numeric actor match, recompute the full selection digest,
and issue a fresh scoped consuming grant for each mutation. Raw body text is not
included in the attestation/ledger metadata.

The operation transport charges every GET/POST/PATCH/DELETE before physical
dispatch, including inherited evidence pagination retries, eligibility/finalizer
reads and reply/assessment writes. Pass the same original caller callback to all
source phases; the wrapper creates no phase deadlines, counter or retry allowance.
Use its inherited evidence methods without an additional `before_read` callback
to avoid double charging. Broker calls, source fences and storage-tail liabilities
must fit the same original host-owned envelope. D cannot consume an A storage-tail
ticket. Expired/exhausted callbacks refuse before transport; late responses refuse
without refund. An ambiguous write may already exist remotely, so reconciliation
must retain the original event, operation and failed attempt witness. Creating a
new budget during restart is unsupported.

Synthetic source and installed-wheel fixtures exercise complete long triggers,
ordered selectors, stable edit lookup, both source fences, secondary source edits
and revoked authorization, whole-operation dispatch accounting, and preserved
legacy behavior. These fixtures do not establish broker deployment, durable
activation-tail recovery, live provider qualification or published product support.
