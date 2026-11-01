# Comment intent proposals

The proposal library is inactive. Existing callers, explicit commands and
review flows retain their behavior; no provider is installed or selected.

An embedding host may construct `CommentObservation` and `ResolvedActionFacts`
from authenticated source/current-head metadata, then call
`review_sensei_github.comment_intent.prepare_comment_proposal`. The result is
either an explicit/static non-executable proposal or a bounded closed-choice
`DecisionRequest`. The library does not send that request. A future host must
first admit extraction against its original shared allowance, provider/model
capabilities, privacy policy, deadline and durable attempt state.

`DecisionProvider.evaluate(request, tracker)` is the typed adapter seam. This
change supplies no implementation or dispatcher. `parse_decision_response`
requires the exact complete question set, permitted choices, original request
digest and configured provider/model. Unknown usage is retained as unknown,
not zero. No generative JSON fallback, confidence threshold or semantic retry
is enabled. The public response schema describes structure; exact dynamic
choices and request identity require the Python parser too.

`complete_comment_proposal` rechecks the current source/state and consumes a
validated typed response. Its output always has `executable == False`.
Static explicit results finish during preparation. Response completion refuses
an explicit preparation because no extraction request exists to bind that
response; it cannot switch an older response into a new source operation.
Current source/state refusals retain precedence.
Actions name existing semantics for future admission, not generated command
text. No returned object is accepted by a command handler or broker as a grant.
The provisional single-clause stance and original UTF-8 span are retained in
the proposal. Hosts must retain the original observation, exact request and
validated response; a proposal or its digest cannot replace those inputs.

Global claims preserve whole-current-PR analysis scope even when one finding is
named. Inline claims inherit the exact authenticated root instance. The
first slice supports canonical displayed six-hex RS IDs and unique 7–40-hex
commit references; unsupported/ambiguous references abstain. It conservatively
abstains on quotations, multiline input, conjunctions and clause punctuation,
incompatible claim/action choices, and non-ASCII letters
or digits, and hidden Unicode controls. Original UTF-8 offsets remain exact, including adjacent symbols.
Tokens explicitly following `commit` must be complete supported identifiers;
missing or malformed tokens clarify rather than losing claimed-change evidence.
Broader language or claim extraction needs separate fixtures and qualification.

An assessment proposal carries pending evidence requirements bound to current
diff/inventory/context policy, each selected original criterion and claimed
commit/current head. None means the code was read, a finding fixed, PR coverage
complete or approval earned. `assess_claim` requires a future general evidence
adapter; `verify` is never a substitute. A negative claim can be assessed without
becoming a risk acceptance. Inline evidence cannot become a global override.

Risk/coverage/media dispositions and recovery remain explicit-only; approval
prose can propose status, not approval. Even maintain/admin and admitted
extraction do not bypass current-head, evidence, native-effect or accounting
gates. Model quality, speed and cost have not been measured. See
[ADR 0121](adr/0121-proposal-only-comment-intent.md) for boundaries and follow-ups.

## Multi-item batch proposals

`review_sensei_github.comment_intent.prepare_comment_batch` prepares an offline
batch from one authenticated original observation and current host facts.
`normalize_decision_groups` in `review_sensei.comment_intent_batch` validates
complete named-choice response groups. `complete_comment_batch` returns a
`CommentBatchProposal` with individually attributable item results.

The first batch grammar uses LF-separated statements, bullets and semicolon
units. A canonical displayed finding ID followed by a colon can label its
explanation. Quote and fence context is tracked across the original comment;
protected and unsupported units remain visible. Indivisible explanations with
multiple finding targets require clarification. Richer sentence segmentation
and generative span extraction are separate work.

Each item has an identity derived from the original authenticated source,
segmentation version and exact original UTF-8 range. Its explanation, references,
claim stance, current finding target, pending evidence and result stay together.
Extraction-array order and a sibling's interpretation do not rename that item.
Different occurrences of identical text have distinct delivery identities.
Equivalent proposed operations can share an `operation_key`; all original
claims, including contradictory evidence, stay in the results. The key is data,
not a durable receipt or an execution grant.

For example, one comment can propose assessment of RS-ABC123, request risk
acceptance for RS-DEF456, give thanks and ask not to verify. These produce
separate assessment, explicit-command-required, no-action and evidence-only
results. An ambiguous reference in one independent item does not redirect a
valid sibling. Restrictions derive from unquoted original text even when the restricting item is static, pending, invalid, or model-misclassified. Explicitly targeted restrictions constrain related findings; targetless PR restrictions constrain their action family. Quoted fragments contribute neither restrictions nor targets. Cross-item qualifiers such as “do not assess either” mark the
plausibly affected requests ambiguous. A verify proposal performs finalization
semantics and cannot supply the code assessment or coverage missing elsewhere.
Global items carry the whole current PR scope; inline items keep their exact
root and cannot use another item to target a different concern.

Legacy explicit parsing applies to the complete original comment. An embedded
command-looking fragment has no standalone command authority. No sliced source
comment is manufactured for planning: each proposal binds the original complete
observation and an independently validated item range.

The complete inventory contains at most ten items and ten reference candidates.
At most forty questions are deterministically packed into at most three requests, with complete
item question sets in each group. Each request keeps the existing sixteen-question,
4096-byte input and 16-KiB envelope bounds. Original source and complete manifest
identity appear in every group; oversized frames are refused without truncation.
Returning requests does not fund them. A future dispatcher must admit their
complete liability under the original shared call/byte/deadline/privacy allowance;
there is no fresh window per item.

Changed source/state or a forged manifest invalidates the shared interpretation.
An incomplete or invalid native group leaves its dependent items pending/refused;
valid groups remain observations and the batch reports incomplete extraction.
Missing answers are not `no_action`; unknown usage stays unknown. Semantically
uncertain, negated or root-conflicting items have individual outcomes. No response
is retried or replaced by another provider. Every item and batch has
`executable == False`; extraction completeness is distinct from assessment,
review coverage, authorization and approval.

This is an offline library extension. No caller, event subscription, provider
transport, persistent per-item receipt, action admission or automatic comment
handling is enabled by these APIs.
