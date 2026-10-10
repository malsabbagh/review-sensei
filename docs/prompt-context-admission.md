# Rendered document context admission

Issue [244](https://github.com/malsabbagh/review-sensei/issues/244), lane E.
`whole-supplemental-rendered-v1` measures the complete provider frame through the
shared service formatter, including scope instructions and the reserved
structural correction. Shared document serialization is coordinator-owned;
`shared-review-documents-v1` preserves every lens association while emitting
identical content and SHA once. A single-lens payload keeps its existing shape.

Only whole supplemental document payloads may be omitted. Explicit/pinned
requirements and contexts without selection provenance remain mandatory in all
active lenses. Instructions, learnings, categories, source context and atomic
evidence remain unchanged. Selection ranks against the actual batch paths and
uses exact UTF-8 rendered bytes, including JSON escaping. Existing qualified
provider capacity uses the configured adapter output-token reserve; unqualified
input stays at 49,152 bytes. At most 65 formatter probes fit the existing
64-unique-document envelope, with original-deadline checks before ranking and
between probes. Expiry is a deadline refusal, not a capacity diagnosis.

The host reserves 8,192 summary bytes before dispatch for diagnostics, including
2,048 bytes for precise pending-work text. Output or diagnostic-inventory bounds
have distinct refusal reasons. A provider response that would exceed the final
summary bound remains pending as a whole; provider prose is never clipped.
The accepted-batch appendix contains original report digests/counts/reasons and
a separate executed selection digest, selected/mandatory/omitted counts and
rendered bytes. It contains no raw documents and excludes planner probes.

An actual accepted batch with complete changed-file coverage can produce a
publishable partial review when document selection is incomplete. That result
cannot replace the completed baseline, overwrite cache authority, close omitted
prior findings, or authorize incremental reuse. The existing authenticated
checkpoint and COMMENT publication carry the summary appendix. Human findings
retain their inventory, and resolving them leaves the durable `review-partial`
automatic-approval blocker. No public schema extension is required.

The exact PR231 fixture reads only the pinned diff at
`bf408b0c070e7404fefcf50eebc104346228662b` and trusted base documents at
`70305207647c22595660e5ebb9e4a33991489ffe`. Its inventory is 73 discovered,
64 selected, nine file-budget omissions, digest
`bd9745c91e42f4edc5e19b3d8101bc6bfa33b4146e3843211b1b5c7fd2fc548f`.
The pinned `docs/architecture.md` alone is 61,136 UTF-8 bytes, digest
`c97cc4c95234b3e4a024714528b306e560c0a8c176060f3f4ddfc62746f4c038`.
It remains a genuine zero-call mandatory refusal under the unqualified cap.
Optional selection and lossless deduplication cannot remove that obligation.
A separately labeled synthetic qualified adapter admits the exact diff in one
179,949-byte request with complete changed-file coverage and partial context
status. This fixture does not qualify any Ollama or GLM model. The generated
73/64/9 fixture with a fitting mandatory core separately exercises partial
checkpoint, restart, COMMENT/readback, cited human resolution and NOAPPROVE.

Normal authenticated work-receipt restart revalidates source and policy identity
and retains original counters/elapsed time. Exhausted-deadline cached
continuation is not qualified here: C owns activation and restore ordering, and
its reviewed integration remains required. Common partition authentication,
aggregate multi-call decision authority, installed-runtime rollout and actual
PR231 provider assessment also remain separate obligations.
