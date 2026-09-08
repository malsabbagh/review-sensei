"""Validate one source-tree evaluation corpus with schema, privacy, and inventory checks."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from review_sensei.evaluation import load_corpus  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "corpus",
        nargs="?",
        type=Path,
        default=ROOT / "evaluation" / "v1" / "corpus.json",
    )
    args = parser.parse_args(argv)
    try:
        corpus = load_corpus(args.corpus)
    except Exception as exc:
        print(f"corpus validation failed: {exc}", file=sys.stderr)
        return 1
    print(f"OK {args.corpus.relative_to(ROOT)}")
    for path in corpus.files:
        print(path.relative_to(corpus.root).as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
