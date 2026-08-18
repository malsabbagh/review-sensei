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
review. A provider/model/configuration change is approved only after three
independent fixture or live runs are recorded with the same corpus and no
regression in the deterministic or quality thresholds. CI remains fixture-only;
live runs are gated, non-secret, and performed outside the CI workflow.

## Rollback

Rollback is code-only: revert the additive fixture provider, evaluator, CLI,
schema, corpus, documentation, and CI changes. No stored state or migration is
created.
