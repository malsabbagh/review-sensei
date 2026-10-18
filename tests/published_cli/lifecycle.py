#!/usr/bin/env python3
"""Run shared lifecycle journeys against the installed distributions only."""

import argparse
import importlib.metadata
import importlib.util
import json
import sys
import unittest
from pathlib import Path

CONTRACTS = (
    "test_approval_lifecycle_contract",
    "test_local_review_contract",
    "test_baseline_recovery_contract",
    "test_provider_deadline_contract",
    "test_openrouter_cli_contract",
    "test_large_public_lifecycle_contract",
    "test_openrouter_metadata_contract",
)


def load_contracts(tests):
    """Load exact files for this one-shot process, replacing cached names.

    Contract classes retain their module globals for the duration of the run.
    Embedders must isolate/restore these module entries, as the bootstrap test
    does; production qualification always invokes this script in a new process.
    """
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    root = tests.resolve()
    lane = (root / "dist_safe").resolve()
    if lane != root / "dist_safe":
        raise ImportError("contract directory escapes the qualification root")
    paths = []
    for name in CONTRACTS:
        path = (lane / (name + ".py")).resolve()
        if path.parent != lane or path.name != name + ".py":
            raise ImportError("contract file escapes its declared path")
        paths.append((name, path))
    # Validate the entire declared inventory before executing any module.
    for name, path in paths:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError("contract module has no file loader")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        suite.addTests(loader.loadTestsFromModule(module))
    return suite


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

    load_error = None
    try:
        suite = load_contracts(tests)
    except Exception as error:
        load_error = type(error).__name__
        suite = unittest.TestSuite()
        rows.append({"id": "suite-load", "status": "FAIL", "kind": "load-error"})
    result = None
    if load_error:
        print("Lifecycle suite load failed: " + load_error, file=sys.stderr)
    else:
        result = unittest.TextTestRunner(verbosity=2, resultclass=Evidence).run(suite)
    if not rows:
        rows.append({"id": "suite-discovery", "status": "FAIL", "kind": "empty-suite"})
        print("Lifecycle suite is empty", file=sys.stderr)
    failed = sum(row["status"] == "FAIL" for row in rows)
    report = {
        "version": args.expected_version,
        "github_version": args.expected_github_version,
        "total": len(rows),
        "pass": len(rows) - failed,
        "fail": failed,
        "rows": rows,
        "tests_run": 0 if result is None else result.testsRun,
        "load_error": load_error,
        "boundary": "installed wheels / synthetic broker and provider only",
    }
    (args.report / "results.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    return (
        0
        if result is not None and result.wasSuccessful() and not failed and rows
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
