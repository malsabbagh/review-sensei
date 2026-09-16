# Evaluation

ReviewSensei ships a small synthetic v1 corpus for reproducible, privacy-safe
regression checks. It is not a benchmark of general model capability.

## Offline fixture mode

From the repository root:

```bash
python scripts/validate_evaluation_corpus.py
env -u OLLAMA_API_KEY review-sensei evaluate --mode fixture \
  --corpus evaluation/v1/corpus.json --output evaluation-report.json
```

Fixture mode reads only the declared corpus files, uses the bounded
`FixtureProvider`, and performs no network access or API-key lookup. The corpus
manifest and report are public v1 schemas. Reports intentionally omit raw
diffs, prompts, responses, secrets, environment values, and monetary cost
claims.

For an exact, standalone fixture-provider check, run the same review entry point
with the pinned fixture model and compare its validated JSON result with the
corpus oracle:

```bash
env -u OLLAMA_API_KEY review-sensei \
  --provider fixture --fixture-response evaluation/v1/responses/off-by-one.json \
  --diff evaluation/v1/diffs/off-by-one.patch --model fixture-v1 \
  --no-learning-proposals --output /tmp/review-sensei-off-by-one.json
cmp /tmp/review-sensei-off-by-one.json \
  evaluation/v1/expected/off-by-one.review.json
```

The explicit `fixture-v1` model is part of the oracle contract; omitting it
would correctly produce a different model field under the normal local
`qwen3.5:4b` default.

## Metrics and limits

The deterministic section measures exact fixture-result rate and expected
contract-rejection rate. The quality section measures actionable precision,
false-positive rate, expected-finding recall, location validity, and category
coverage. Finding matching is one-to-one by canonical path, changed line,
category, and normalized body terms. Provider calls, UTF-8 prompt/response
bytes, elapsed time, and `ceil(bytes / 4)` are non-monetary proxies.

The corpus is a four-category regression sentinel plus an acceptable clean case
and malformed-output rejection. It cannot establish broad model quality, and
transparent body-term matching can under-credit valid paraphrases. Prompt
injection in source/comments remains untrusted model input; privacy scanning
is a defense-in-depth check, not proof of provenance.

## Live walkthrough and egress warning

Live evaluation is deliberately opt-in and should be run only against reviewed
synthetic data:

```bash
review-sensei evaluate --mode live --corpus evaluation/v1/corpus.json \
  --provider ollama --base-url http://127.0.0.1:11434/api \
  --model qwen3.5:4b --provider-version local-ollama-1 \
  --allow-live-model --output live-report.json
```

Loopback endpoints do not require the egress acknowledgement. Any other host
is remote and requires the additional `--allow-data-egress` flag before the
provider is constructed. Never put either acknowledgement or provider secrets
in CI. Review the provider's current retention, training, residency, and
deletion terms before sending source data.

## Threshold and provider-change policy

Corpus, matching, threshold, and report-schema changes require maintainer
review. Fixture runs are deterministic engine checks only and can never approve
a real model, prompt, generation-setting, or routing promotion. Rerunning
fixture evaluation after failing live evidence cannot convert that failure into
approval.

Such a promotion requires a validated `promotion-record` document (the packaged
`promotion-record` schema) bound to passing live evaluation reports. The record
carries:

- `engine_digest`: SHA-256 of package identity, `ReviewService` identity,
  matching algorithm, output-correction attempt budget, location enforcement,
  and `ReviewLimits` ceilings
- `prompt_digest`: SHA-256 of packaged or custom stage templates and the full
  category definitions those templates interpolate
- `configuration_digest` and `corpus_digest` copied from the reports
- provider/model identity or observed revision
- `run_count`, `evaluated_at`, and reproducibility settings
- explicit `supported`, `insufficient`, or `unsupported` status
- a rollback decision (`revert-to-baseline`, `hold`, or `none`)

Only `status=supported` can authorize promotion, and it requires at least three
independent live runs that passed the corpus quality thresholds. Independence is
the unique `run.invocation_id` stamped by each `evaluate` invocation; copies
that only change elapsed-time metrics are not independent. Fixture provider
aliases cannot produce `supported`. Incomplete records (missing
rollback decision, missing status, `run_count < 3` with `supported`, digest
mismatch, or missing reproducibility) fail closed.

`require_supported_promotion(record, reports)` is the documented, fail-closed
gate for release and documentation workflows that promote a model, prompt,
generation setting, or routing configuration. Ordinary CI must not call it to
mint approval, and it never contacts a live provider.

### Operator commands

Collect at least three independent live reports outside CI, then emit or
validate the promotion record. These commands read report files only; they do
not add live-model or secret flags to CI:

```bash
review-sensei evaluate --mode live --corpus evaluation/v1/corpus.json \
  --provider ollama --base-url http://127.0.0.1:11434/api \
  --model qwen3.5:4b --provider-version local-ollama-1 \
  --allow-live-model --output live-report-1.json

review-sensei promotion emit \
  --report live-report-1.json \
  --report live-report-2.json \
  --report live-report-3.json \
  --observed-revision local-ollama-1 \
  --evaluated-at 2026-09-16T00:00:00Z \
  --reproducibility-json '{"seed":"fixed","temperature":0}' \
  --output promotion-record.json

review-sensei promotion validate --require-supported \
  --record promotion-record.json \
  --report live-report-1.json \
  --report live-report-2.json \
  --report live-report-3.json
```

The same emit/validate flow is available from
`python scripts/validate_promotion_record.py`. Reproducibility settings may be
passed inline with `--reproducibility-json` or loaded from a bounded JSON file
with `--reproducibility-file`. The record is metadata about the evaluation, not
the run evidence itself: operators must retain exact run outputs and provider
terms/egress approval separately.

Symbol-aware source context stays opt-in until evaluation under
[#33](https://github.com/malsabbagh/review-sensei/issues/33) measures
precision, recall, and usage impact on cross-file cases. Enabling
`--enable-symbol-context` without that evaluation must not be treated as
default policy.

CI remains fixture-only. Live runs are gated, non-secret, and performed outside
the CI workflow against reviewed synthetic or explicitly authorized data. The
repository does not claim hosted-provider or GitHub evidence until that
separately retained evidence is available.

## Learning effect comparison

Fixture evaluation can compare the same cases with and without selected
approved learnings:

```bash
review-sensei evaluate --mode fixture --corpus evaluation/v1/corpus.json \
  --compare-learnings --learning-root /path/to/target-branch \
  --output learning-effect.json
```

The comparison document sets `causal_claim` to false. Precision, recall, and
false-positive deltas are estimates from the synthetic corpus, not proof that
production feedback caused a quality change. Opt-in `learning-feedback`
records distinguish useful, incorrect, obsolete, and unverified findings;
absence of feedback is not counted as approval and cannot enter review
prompts.

Named provider profiles (`local-private`, `fast-triage`, `deep-verification`)
follow the same rule: `validate_profile_promotion` rejects fixture aliases and
requires a `supported` promotion record whose provider and model match the
profile. Selecting `--profile fast-triage` or `--profile deep-verification` for
`evaluate --mode live` is remote egress and requires `--allow-data-egress`
even when `--base-url` still points at loopback.

## Rollback

Rollback is code-only: revert the additive fixture provider, evaluator, CLI,
schema, corpus, documentation, and CI changes. No stored state or migration is
created.
