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
