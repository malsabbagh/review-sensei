#!/usr/bin/env python3
"""Run shared lifecycle journeys against the installed distributions only."""

import argparse
import concurrent.futures
import importlib.metadata
import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path

CONTRACTS = (
    "test_approval_lifecycle_contract",
    "test_local_review_contract",
    "test_baseline_recovery_contract",
    "test_provider_deadline_contract",
    "test_conversation_deadline_contract",
    "test_provider_common_flow_contract",
    "test_openrouter_cli_contract",
    "test_large_public_lifecycle_contract",
    "test_public_feedback_continuation_contract",
    "test_original_operation_context_contract",
    "test_broker_dispatch_control_contract",
    "test_initial_assessment_tail_contract",
    "test_rich_public_writer_contract",
    "test_rich_feedback_contract",
    "test_openrouter_metadata_contract",
    "test_default_publication_recovery_contract",
    "test_default_human_inventory_contract",
    "test_override_metadata_contract",
    "test_hosted_history_readers_contract",
    "test_hosted_history_independent_contract",
    "test_hosted_graphql_qualification",
    "test_openrouter_runtime_qualification_contract",
)


def load_contracts(tests, selected=None):
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
    selected = CONTRACTS if selected is None else tuple(selected)
    if (
        not selected
        or len(set(selected)) != len(selected)
        or set(selected) - set(CONTRACTS)
    ):
        raise ValueError("invalid lifecycle contract selection")
    for name, path in paths:
        if name not in selected:
            continue
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError("contract module has no file loader")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        # Keep each declared module's load_tests additions in its own group;
        # imported incident regressions need not use the module's ID prefix.
        suite.addTest(loader.loadTestsFromModule(module))
    return suite


def assert_installed(tests, version, github_version):
    # Test code is supplied by the qualification ref; runtime code must come
    # from these exact installed wheels. Neither src directory is added.
    tests = Path(tests)
    sys.path.insert(0, str(tests / "dist_safe"))
    sys.path.insert(0, str(tests))
    from packaging_guard import assert_distribution_import

    assert_distribution_import()
    import review_sensei_github

    origin = Path(review_sensei_github.__file__).resolve()
    assert origin.is_relative_to(Path(sys.prefix).resolve()), origin
    assert importlib.metadata.version("review-sensei") == version
    assert importlib.metadata.version("review-sensei-github") == github_version


def test_ids(suite):
    identities = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            identities.extend(test_ids(item))
        else:
            identities.append(item.id())
    return identities


def execute_suite(suite):
    rows = []
    executed = []

    class Evidence(unittest.TextTestResult):
        def startTest(self, test):
            executed.append(test.id())
            super().startTest(test)

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

    log = io.StringIO()
    result = unittest.TextTestRunner(verbosity=2, resultclass=Evidence, stream=log).run(
        suite
    )
    return {
        "rows": rows,
        "executed_ids": executed,
        "tests_run": result.testsRun,
        "successful": result.wasSuccessful(),
        "log": log.getvalue(),
    }


def run_contract(tests, name, version, github_version):
    assert_installed(tests, version, github_version)
    return execute_suite(load_contracts(Path(tests), (name,)))


def validate_execution(record, expected):
    if (
        not isinstance(record, dict)
        or not expected
        or len(set(expected)) != len(expected)
        or record.get("executed_ids") != expected
        or type(record.get("tests_run")) is not int
        or record["tests_run"] != len(expected)
        or not isinstance(record.get("rows"), list)
        or not record["rows"]
    ):
        raise ValueError("lifecycle worker did not execute its exact inventory")
    rows = record["rows"]
    if any(row.get("status") not in {"PASS", "FAIL"} for row in rows):
        raise ValueError("invalid lifecycle worker result")
    if record.get("successful") is True and (
        len(rows) != len(expected) or {row.get("id") for row in rows} != set(expected)
    ):
        raise ValueError("lifecycle worker omitted or duplicated test evidence")
    return record.get("successful") is True and all(
        row["status"] == "PASS" for row in rows
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--expected-github-version", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    args.report.mkdir(parents=True, exist_ok=False)
    tests = Path(__file__).resolve().parents[1]
    assert_installed(tests, args.expected_version, args.expected_github_version)
    rows = []
    executed = []
    successful = False
    load_error = None
    try:
        suite = load_contracts(tests)
    except Exception as error:
        load_error = type(error).__name__
        suite = unittest.TestSuite()
        rows.append({"id": "suite-load", "status": "FAIL", "kind": "load-error"})
    if load_error:
        print("Lifecycle suite load failed: " + load_error, file=sys.stderr)
    else:
        inventory = test_ids(suite)
        if inventory:
            try:
                by_contract = {
                    name: test_ids(group)
                    for name, group in zip(CONTRACTS, suite, strict=True)
                }
                # Processes isolate module globals and installed imports. Keep
                # the complete inventory and the outer 300-second deadline.
                with concurrent.futures.ProcessPoolExecutor(max_workers=2) as pool:
                    futures = [
                        pool.submit(
                            run_contract,
                            str(tests),
                            name,
                            args.expected_version,
                            args.expected_github_version,
                        )
                        for name in CONTRACTS
                    ]
                    successful = True
                    for name, future in zip(CONTRACTS, futures):
                        record = future.result()
                        expected = by_contract[name]
                        successful = validate_execution(record, expected) and successful
                        rows.extend(record["rows"])
                        executed.extend(record["executed_ids"])
                        sys.stderr.write(record["log"])
                if sorted(executed) != sorted(inventory):
                    raise ValueError("lifecycle inventory is incomplete")
            except Exception as error:
                successful = False
                rows.append(
                    {
                        "id": "worker-execution",
                        "status": "FAIL",
                        "kind": type(error).__name__,
                    }
                )
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
        "tests_run": len(executed),
        "executed_ids": executed,
        "workers": 2,
        "load_error": load_error,
        "boundary": "installed wheels / synthetic broker and provider only",
    }
    (args.report / "results.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    return 0 if successful and not failed and rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
