# Published consumer entry contracts

The default six-suite harness selects the current installed `review-run` and
`reply-run` entry points. `operations` runs the original 39 scenario identities
and 15 additional current-entry/refusal cases. Product code must load from the
isolated exact wheels; only test fixture modules load from the qualification
checkout. No `src` directory enters the import path.

`run_fixture.py` reuses the existing synthetic GitHub and broker response
fixtures. It invokes the installed CLI, real broker client, single caller
transport, authority parser, session ledger, engine, actual Ollama adapter,
review/reply publisher and finalizer. Only external HTTP responses, transport
endpoint construction and the clock are supplied by the fixture. It does not
replace `publish_review`, `generate_and_publish_reply`, operation admission,
session activation, engine execution or a lifecycle continuation. A synthetic
broker response does not qualify production Worker signatures or deployment.

## Original oracle mapping

Every `complete`/`partial` suffix in the original 39 scenarios is retained.
Partial cases now obtain real partial coverage from two review batches and a
one-call provider budget; a handcrafted partial result is no longer supplied
directly to a mocked publisher.

| Original case family | Current oracle |
| --- | --- |
| `workflow-token-missing-appid-{empty,99}-{complete,partial}` | `review-run` refuses missing workflow read capability before any external request, inference or publication. Exact startup diagnostic is required. |
| `workflow-token-present-appid-{empty,99}-{complete,partial}` | Real broker-returned App identity authorizes the current root regardless of the ambient App-ID hint. Actual provider, durable publication and terminal accounting run; partial coverage withholds native approval. |
| `readback-matching-{complete,partial}` | Create a genuine accepted current root, then authenticate a fresh run against the same head and proven stopped predecessor. Retained completion must close the new owner without another provider call or approval. |
| `readback-mismatch-{complete,partial}` | Corrupt the genuine root's integrity digest. The current native ledger refuses before new inference or native review writes. |
| `readback-{403,wrong-type,timeout}-{complete,partial}` | Inject denial, malformed listing or transport timeout at actual root discovery. Require the requested fault to be observed and no inference. |
| `readback-second-page-{matching,mismatch,absent}-{complete,partial}` | Place current retained evidence, corrupt evidence or no evidence after a full page of unrelated PR-associated comments. Require page two to be reached; actual current-root completion/refusal replaces the old mocked continuation count. With no root, actual enrollment, publication and terminal completion must succeed. |
| `readback-second-page-{403,malformed,timeout}-{complete,partial}` | Reach page two and its exact injected failure; refuse before inference or native publication. |
| `readback-scan-limit-{complete,partial}` | An endless complete page must stop within the unchanged ten-request startup discovery envelope; no provider call or native approval. |
| `readback-oversized-page-{complete,partial}` | Return more rows than requested at discovery; malformed evidence must not authorize provider/publication work. |
| `readback-shared-deadline-{complete,partial}` | Advance the external clock past the actual caller request's original deadline. The current root has no replacement phase allowance; an expired response cannot fund progress. |
| `non-actions-no-token` | The current run-owned route requires authenticated Actions identity and read capability. The old local `publish` continuation expectation is retired. Local engine behavior remains covered by the console/component suites. |
| `disabled-entry-no-token` | Missing current read authority refuses instead of allowing a disabled legacy gate to bypass run ownership. A supplementary authenticated disabled-policy reply case separately reaches the trusted-policy refusal. |
| `wrong-author-mismatching-record` | A foreign author's historical operation marker cannot supply current authority. A fresh authenticated root, engine and partial publication must execute; the foreign marker is never replayed as an accepted result. |

Additional reply cases reach actual typed action intent, provider entry,
publication journal and terminal completion. Missing capability, mismatched
grant purpose/head, overlapping writers, source author mismatch, unfunded
allocation and disabled trusted policy refuse before inference. Incomplete
provider output, unknown reply delivery and unknown reaction cleanup remain
pending; a proven stopped successor cannot reinfer or duplicate the reply.
Unknown write charges remain retained. Three explicit Actions `review`,
`publish` and `reply` cases require the retirement diagnostic before effects.

The shared, unchanged `operation_state` suite retains its 17 low-level component
contracts. Those component passes are not evidence that a current public entry
point ran; the new operations cases provide that separate evidence. Other
console, transport, boundary and lifecycle suites remain selected and unchanged.

## Historical major-validator baseline

`--historical-baseline` is an explicit regression mode restricted to exactly
engine `0.6.23`, adapter `0.1.1`, both existing pinned wheel digests, and the five
explicit original suites. Invalid combinations refuse before installation. It
selects `historical_operations.py` and the unchanged `operation_child.py`:

- Original operations SHA256: `e42b435753425f9a055ac9a14fc3c2b5bba2f17a0541c3af9458e009685b84d9`.
- Original child SHA256: `e9b5f50e1ff777c342031d768fef9e18d9ab718ffb462624808e78f442bd587a`.

Receipts record the mode and fixture identities. Default current qualification
never auto-detects an old version or falls back after a failure. The current
published-consumer workflow has no baseline-mode flag. The historical baseline
does not assert current lifecycle support for immutable old packages.

## Recovery of the published pair

The original failed run `40358649231` remains failed evidence. After normal
review and merge of the corrected harness, only the sole publisher dispatches
`published-cli.yml` once for the same immutable `0.8.14` / `0.3.20` pair. All six
suites must pass on all four platform/Python lanes under their existing bounds.
Each installed wheel hash must match the retained original publication proof.
The manual dispatch gate does not repeat the automatic signed-tag/publication
checks; join its corrected-harness source and receipts to the original signed
source, tag objects, successful publisher attempts and verified PyPI provenance.
Neither a local fixture pass nor this harness correction proves historical PR32
recovery, live provider quality, Worker activation or release cutover completion.
