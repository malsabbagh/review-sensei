# ReviewSensei evaluation corpus v1

This directory contains the CC0-1.0 synthetic corpus used by ReviewSensei's
deterministic fixture evaluation. All source files, diffs, provider responses,
and expected results are synthetic and intentionally contain no private,
production, personal, or secret material.

Run the corpus validator:

```bash
python scripts/validate_evaluation_corpus.py
```

Run the deterministic fixture evaluation:

```bash
python -m review_sensei evaluate --mode fixture --corpus evaluation/v1/corpus.json
```

For a standalone exact review oracle, pin the fixture model explicitly:

```bash
review-sensei --provider fixture \
  --fixture-response evaluation/v1/responses/off-by-one.json \
  --diff evaluation/v1/diffs/off-by-one.patch --model fixture-v1 \
  --no-learning-proposals --output /tmp/review-sensei-off-by-one.json
cmp /tmp/review-sensei-off-by-one.json \
  evaluation/v1/expected/off-by-one.review.json
```

The manifest in `corpus.json` must list every file in this directory. Adding,
removing, renaming, or changing an expectation is a corpus change and must be
reviewed like any contract or threshold change.
