#!/usr/bin/env python3
"""Run shared lifecycle journeys against the installed distributions only."""

import argparse
import concurrent.futures
import importlib.metadata
import importlib.util
import io
import json
import multiprocessing
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
    suite.contract_inventory = {}
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
        group = loader.loadTestsFromModule(module)
        suite.contract_inventory[name] = test_ids(group)
        suite.addTest(group)
    return suite


def assert_installed(tests, version, github_version):
    # Test code is supplied by the qualification ref; runtime code must come
    # from these exact installed wheels. Neither src directory is added.
    tests = Path(tests)
    for path in (tests / "dist_safe", tests):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
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

    def note(test, status, kind=None):
        row = next((row for row in rows if row["id"] == test.id()), None)
        if row is None:
            row = {"id": test.id(), "status": status}
            rows.append(row)
        if status == "FAIL":
            row["status"] = status
            row.setdefault("kind", kind)
        return row

    class Evidence(unittest.TextTestResult):
        def startTest(self, test):
            executed.append(test.id())
            super().startTest(test)

        def addSuccess(self, test):
            super().addSuccess(test)
            note(test, "PASS")

        def addFailure(self, test, err):
            super().addFailure(test, err)
            note(test, "FAIL", "assertion")

        def addError(self, test, err):
            super().addError(test, err)
            note(test, "FAIL", "error")

        def addSkip(self, test, reason):
            super().addSkip(test, reason)
            note(test, "FAIL", "skipped")

        def addExpectedFailure(self, test, err):
            super().addExpectedFailure(test, err)
            note(test, "FAIL", "expected-failure")

        def addUnexpectedSuccess(self, test):
            super().addUnexpectedSuccess(test)
            note(test, "FAIL", "unexpected-success")

        def addSubTest(self, test, subtest, err):
            super().addSubTest(test, subtest, err)
            if err is not None:
                row = note(test, "FAIL", "subtest")
                row["subtest_failures"] = row.get("subtest_failures", 0) + 1

    log = io.StringIO()
    result = unittest.TextTestRunner(verbosity=2, resultclass=Evidence, stream=log).run(
        suite
    )
    return {
        "rows": rows,
        "executed_ids": executed,
        "tests_run": result.testsRun,
        "successful": result.wasSuccessful()
        and all(row["status"] == "PASS" for row in rows),
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
    if any(
        not isinstance(row, dict) or row.get("status") not in {"PASS", "FAIL"}
        for row in rows
    ):
        raise ValueError("invalid lifecycle worker result")
    if len(rows) != len(expected) or {row.get("id") for row in rows} != set(expected):
        raise ValueError("lifecycle worker omitted or duplicated test evidence")
    if type(record.get("successful")) is not bool or record["successful"] != all(
        row["status"] == "PASS" for row in rows
    ):
        raise ValueError("lifecycle worker success flag conflicts with its evidence")
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
    inventory = []
    confirmed_contracts = []
    workers_started = False
    worker_contract = None
    stage = "inventory"
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
                by_contract = suite.contract_inventory
                if (
                    set(by_contract) != set(CONTRACTS)
                    or any(not ids for ids in by_contract.values())
                    or len(set(inventory)) != len(inventory)
                ):
                    raise ValueError("declared lifecycle inventory is incomplete")
                if [
                    identity for name in CONTRACTS for identity in by_contract[name]
                ] != inventory:
                    raise ValueError(
                        "declared lifecycle inventory differs from discovery"
                    )
                # Processes isolate module globals and installed imports. Keep
                # the complete inventory and the outer 300-second deadline.
                with concurrent.futures.ProcessPoolExecutor(
                    max_workers=2, mp_context=multiprocessing.get_context("spawn")
                ) as pool:
                    stage = "worker-pool"
                    workers_started = True
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
                        stage = "worker-result"
                        worker_contract = name
                        record = future.result()
                        expected = by_contract[name]
                        successful = validate_execution(record, expected) and successful
                        confirmed_contracts.append(name)
                        rows.extend(record["rows"])
                        executed.extend(record["executed_ids"])
                        sys.stderr.write(record["log"])
                if sorted(executed) != sorted(inventory):
                    raise ValueError("lifecycle inventory is incomplete")
            except Exception as error:
                successful = False
                rows.append(
                    {
                        "id": "inventory-discovery"
                        if stage == "inventory"
                        else "worker-execution",
                        "status": "FAIL",
                        "kind": type(error).__name__,
                        "contract": worker_contract,
                        "stage": stage,
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
        "tests_run": None
        if workers_started and len(confirmed_contracts) != len(CONTRACTS)
        else len(executed),
        "known_tests_run": len(executed),
        "executed_ids": executed,
        "expected_inventory": inventory,
        "expected_contracts": list(CONTRACTS),
        "unconfirmed_contracts": [
            name for name in CONTRACTS if name not in confirmed_contracts
        ]
        if workers_started
        else [],
        "execution_complete": bool(inventory)
        and sorted(executed) == sorted(inventory)
        and len(confirmed_contracts) == len(CONTRACTS),
        "workers_started": workers_started,
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
