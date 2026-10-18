#!/usr/bin/env python3
"""Run shared lifecycle journeys against the installed distributions only."""

import argparse
import importlib.metadata
import json
import sys
import unittest
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--expected-github-version", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    args.report.mkdir(parents=True, exist_ok=False)
    # Test code is supplied by the qualification ref; runtime code must come
    # from these exact installed wheels. Neither src directory is added.
    tests = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(tests / "dist_safe"))
    sys.path.insert(0, str(tests))
    from packaging_guard import assert_distribution_import

    assert_distribution_import()
    import review_sensei_github

    origin = Path(review_sensei_github.__file__).resolve()
    assert origin.is_relative_to(Path(sys.prefix).resolve()), origin
    assert importlib.metadata.version("review-sensei") == args.expected_version
    assert (
        importlib.metadata.version("review-sensei-github")
        == args.expected_github_version
    )
    rows = []

    class Evidence(unittest.TextTestResult):
        def addSuccess(self, test):
            super().addSuccess(test)
            rows.append({"id": test.id(), "status": "PASS"})

        def addFailure(self, test, err):
            super().addFailure(test, err)
            rows.append({"id": test.id(), "status": "FAIL", "kind": "assertion"})

        def addError(self, test, err):
            super().addError(test, err)
            rows.append({"id": test.id(), "status": "FAIL", "kind": "error"})

        def addSkip(self, test, reason):
            super().addSkip(test, reason)
            rows.append({"id": test.id(), "status": "FAIL", "kind": "skipped"})

        def addSubTest(self, test, subtest, err):
            super().addSubTest(test, subtest, err)
            if err is not None:
                rows.append({"id": str(subtest), "status": "FAIL", "kind": "subtest"})

    loader = unittest.TestLoader()
    suite = loader.loadTestsFromNames(
        ["test_approval_lifecycle_contract", "test_local_review_contract"]
    )
    result = unittest.TextTestRunner(verbosity=2, resultclass=Evidence).run(suite)
    failed = sum(row["status"] == "FAIL" for row in rows)
    report = {
        "version": args.expected_version,
        "github_version": args.expected_github_version,
        "total": len(rows),
        "pass": len(rows) - failed,
        "fail": failed,
        "rows": rows,
        "boundary": "installed wheels / synthetic broker and provider only",
    }
    (args.report / "results.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    return 0 if result.wasSuccessful() and not failed and rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
