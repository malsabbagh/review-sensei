# ReviewSensei product, issue and source audit — 2026-09-22

This is dated audit evidence, not a release/support declaration. Direction and
priorities: [product direction](../product-direction.md) and
[roadmap #167](https://github.com/malsabbagh/review-sensei/issues/167).

## Evidence identity and scope

| Item | Observed value |
| --- | --- |
| Main source | `1d1f9389c09a1e575b292029c013ea3dbc9af516` |
| Main CI | [35728370094](https://github.com/malsabbagh/review-sensei/actions/runs/35728370094), completed successfully |
| Distribution artifact | `10694357683`, `review-sensei-distributions` |
| Downloaded ZIP SHA-256 | `13f9fdd6e6eb3e01b3efe5e7a6f9646294ab07644f15e22fb63e51e0b9cad23b` |
| Inspected contents | CI wheel and source distribution, both version `0.6.0` |
| Workflow channel read-back | `v5` annotated object `e32359eafe7f26d9c711893d2e5d42552a7244fc` |
| Peeled workflow commit | `575fa5d83f09c2fd0a83a5b5c07574051aac6457` |
| Pending F7 PR | [#155](https://github.com/malsabbagh/review-sensei/pull/155), audited head `c8251e24194f3715e97f2262d7da369b4437e47d` |
| Recorded candidate CI | [35739014043](https://github.com/malsabbagh/review-sensei/actions/runs/35739014043), required checks failed at the audit read-back |

The artifact ZIP digest was checked against GitHub's returned artifact digest.
The wheel was installed in a separate virtual environment. Its runtime
JSON-schema/cryptography dependencies came from the preinstalled environment;
no live provider account or credentials were used. The source distribution was
used for source inspection and its packaged tests, not imported as the engine.

The audit reviewed all 22 initially open issues and the recently closed #146,
alongside the main source distribution, current runner inputs and pending PR
metadata. It did not execute the full repository suite: the source distribution
contains only distribution-safe/downstream tests, not the complete checkout test
suite. The successful main CI above is independently observed upstream evidence,
not a claim that this audit reran it.

## Tests actually rerun

With the CI wheel installed outside the source directory, from its extracted
source-distribution root:

```bash
REVIEWSENSEI_CHECKOUT_ROOT="$PWD" /path/to/venv/bin/python \
  -m unittest discover -s tests/dist_safe -v
REVIEWSENSEI_CHECKOUT_ROOT="$PWD" /path/to/venv/bin/python \
  -m unittest discover -s tests/downstream -v
```

Results: **8 distribution-safe tests passed; 10 downstream tests passed.** The
installed-package import guard remained active. These tests do not establish
correct approval eligibility or full workflow/native-platform acceptance.

## Real publication-boundary probes

The [reproduction script](2026-09-22-approval-probe.py) loads the actual embedded
`example-partial` JSON from `docs/site/examples/index.html` and uses the installed
`ReviewPublisher`/`ReviewApprovalFinalizer`. Only external GitHub HTTP is replaced
with the repository's `_ObservedGitHub` fake. There are no live GitHub review
writes and no model calls. This is not a full service/application/workflow test.

```bash
/path/to/venv/bin/python docs/audits/2026-09-22-approval-probe.py \
  --source-root /path/to/extracted/review_sensei-0.6.0
```

Run against the pinned artifact above. The script reports observed events; it is
not a permanent test expecting these bugs to survive future fixes. In particular,
legacy-selection behavior may change in the replacement release. A different
installed wheel does not inherit this audit's source/CI provenance.

| Input | Explicit legacy policy | Explicit merge-focused policy |
| --- | --- | --- |
| Complete fixture control | `COMMENT`, `APPROVE` | `COMMENT`, `APPROVE` |
| Partial status | `COMMENT`, `APPROVE` | `COMMENT`, `APPROVE` |
| Incomplete status | `COMMENT`, `APPROVE` | `COMMENT`, `APPROVE` |
| Summary-only status | `COMMENT`, `APPROVE` | `COMMENT`, `APPROVE` |
| Actual website partial JSON, including incomplete coverage | `COMMENT`, `APPROVE` | `COMMENT`, `APPROVE` |
| Complete OpenRouter result without qualification evidence | `COMMENT`, `APPROVE` | `COMMENT`, `APPROVE` |

Additional controls: explicit `auto_approve=false` emitted only `COMMENT`; a stale
head emitted no review events. The default policy constructor returned `legacy`.

The incomplete and unqualified approvals reproduce existing P0 issues
[#114](https://github.com/malsabbagh/review-sensei/issues/114) and
[#115](https://github.com/malsabbagh/review-sensei/issues/115). Merely selecting
merge-focused does not repair them. The valid controls also show why the fix
must preserve independent approval/write controls rather than disable everything.

The current finalizer consumes enablement and blocker state, without the trusted
completeness/coverage/qualification context required by these issues. Reuse the
existing conservative eligibility evaluator at the actual emission boundary and
preserve trustworthy exact-head eligibility through recovery/delayed finalization.

## Other current findings

The hosted OpenRouter target pairs were:

```text
anthropic/claude-3.5-sonnet + anthropic
openai/gpt-4o-mini + openai
deepseek/deepseek-v4.1-flash + deepseek
```

The qualification slice was `anthropic/claude-3.5-haiku + anthropic` at
`https://openrouter.ai/api/v1`. The intersection was empty (#118). This describes
the checked-in sets, not whether those models remain available from providers.
Target selection must verify current provider capability before live use.

`OpenRouterRoutingPolicy(upstream_provider="deepinfra/turbo")` was rejected (#121).
`ProviderResponse` fields were `text`, `provider`, `model`, `limits`, `revision`;
there was no reported-usage field (#120). Static adapter inspection showed an
observed model string taking precedence over `system_fingerprint` and being
stored as `revision` (#119). That identity behavior was inspected, not separately
exercised through a live provider request.

The current public v5 runner declares `model`; the historical v4 missing-input
incident should be retained as a regression, not called the current channel
failure. It does not declare pending #155's `review_mode`. Setup schema v4 and
workflow tag v5 are different axes. Neither tag movement nor source version
0.6.0 proves final deployed compatibility or model qualification.

The packaged site now credits OpenRouter CLI/evaluate/npx/workflow implementation,
but labels the hosted allowlisted path `supported` alongside `experimental`.
Source-file references and allowlist membership are not live qualification or
installed-release evidence. Its partial-review example still claims withheld
approval despite the probe above. Some cleanup content already exists; the
manual Actions instructions and equivalent complete no-JavaScript onboarding
remain acceptance work. Production rendering was not examined.

## Full issue disposition

All entries remain open unless explicitly stated. Existing acceptance scopes are
retained or refreshed; no issue was closed merely for age. A roadmap is not a new
implementation epic or an extra dependency for closing these tickets.

| Issue | Disposition / remaining owner |
| --- | --- |
| #91 | Refreshed website epic: credit delivered routes; remaining #124–#130; truthful unqualified status is a valid finish. |
| #92 | Refreshed OpenRouter epic: credit existing adapter; separate runtime, target/metadata and operator-evidence lanes. |
| #99 | Refreshed acceptance gate: exact retained qualification and installed/deployed evidence, not another harness build. |
| #114 | Updated P0 reproduction on current installed artifact, both policy modes, including exact site JSON. |
| #115 | Updated P0 reproduction and trusted qualification/finalization boundary. |
| #116 | Updated current v5 facts; historical model-input mismatch remains a fixture; candidate review-mode compatibility stays open. |
| #117 | Retained: complete installed-artifact operation/control matrix is broader than the 18 smoke tests rerun here. |
| #118 | Retained, current target-set mismatch independently reproduced; select one target without claiming support. |
| #119 | Retained, current identity/fingerprint loss found in adapter source; keep requested and observed identities separate. |
| #120 | Retained, response contract still lacks reported usage; unknown is not zero or an inferred cost. |
| #121 | Retained, endpoint-variant rejection independently reproduced; no privacy/fallback relaxation. |
| #122 | Retained operator task: three independent qualifying live runs and checked original evidence; not performed by this audit. |
| #123 | Refreshed current channel/candidate/rollback facts and explicit operator authority; reuse matching evidence across parents. |
| #124 | Retained: source/distribution/workflow/qualification states must be independent. |
| #125 | Retained: validate claims against retained authoritative evidence, not existence/boolean/version assertions. |
| #126 | Refreshed: old missing-CLI copy is repaired; unsupported hosted-support and partial-approval claims remain. |
| #127 | Refreshed current source/version/contract baseline and complete executable first-review instructions. |
| #128 | Retained: credit existing cleanup sections; finish diagnostic repair and independent-control guidance. |
| #129 | Retained: abbreviated noscript fallback is not equivalent complete onboarding; rendered behavior remains to test. |
| #130 | Retained final six-route desktop/mobile/keyboard/no-JavaScript evidence; not rendered in this audit. |
| #136 | Refreshed accepted replacement-default direction and credited existing convergence foundations; #146 owns finite remaining acceptance. |
| #161 | Retained full feature contract; title marks the next P1 feature. Local Ollama/Jev interface remains planned, not implemented. |
| #146 | Reopened and retitled: final default PR is unmerged at audit; release, maintainer migration and rollback remain part of its original contract. |

New [#167](https://github.com/malsabbagh/review-sensei/issues/167) indexes priorities,
source responsibilities, evidence and scope boundaries. No duplicate runtime
implementation epic was added. Twelve existing issues were updated, including
one reopened issue; the remaining reviewed tickets retain valid outstanding work.

## Limits and next action

No live inference, paid qualification, registry publication, production App or
Worker deployment, tag movement, protection-rule edit, automated merge or
production-site/browser acceptance was performed. No inference-provider privacy
or performance claim was validated merely by source inspection. No runtime safety
fix or DecisionProvider implementation is contained in the direction change.

First fix #114/#115 with captured real-path regressions. Finish the already-chosen
#146/#155 cutover without reimplementing foundations. Develop #161 through its
small existing slices; correct website claims independently. Preserve the original
finite acceptance boundaries rather than adding a new blocker after every fix.
