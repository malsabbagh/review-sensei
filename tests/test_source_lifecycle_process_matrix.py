"""G27 ordinary CLI checkpoints across real interpreter boundaries.

Only synthetic remote JSON and fixture checkout files cross processes. No caller,
result proof, cancellation witness or epoch capability is serialized. The
pending-receipt interruption is a landed business effect without terminal receipt;
the real classifier conservatively calls it started work. Terminal cold entry is
an ordinary bootstrap refusal, not a claimed successful terminal replay route.
A stopped Python process is not promoted to a production cessation/non-dispatch receipt.
"""

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from fake_github_http import json_response
from review_sensei_github import cli as package_cli
from review_sensei_github.session_ledger import parse_session_comment

from review_sensei.api_work import ApiRunAccounting
from tests import test_action_run as fixtures
from tests import test_finding_dictionary_restart as persisted

LIMIT = 512


def record(fixture):
    return (
        parse_session_comment(fixture.comment["body"], identity=fixtures.IDENTITY)
        if fixture.comment
        else None
    )


def snapshot(fixture):
    selected = record(fixture)
    state = (
        ApiRunAccounting.from_document(selected.api_run_accounting)
        if selected
        else None
    )
    return {
        "root": selected.to_dict() if selected else None,
        "business_writes": fixture.business_writes,
        "provider_calls": fixture.provider_calls,
        "closure_writes": fixture.closure_writes,
        "trace": fixture.trace,
        "limit": state.configuration.limit if state else None,
        "remaining": state.remaining if state else None,
        "confirmed": state.confirmed if state else None,
        "uncertain": state.uncertain if state else None,
        "reserved": state.reserved_undispatched if state else None,
        "prior": state.prior_nonstartup_liability if state else None,
        "startup": state.startup.liability if state else None,
        "argv": fixture.observed_argv,
        "environment": fixture.observed_environment,
    }


def save(fixture, directory):
    persisted.save_remote(fixture, directory / "remote.json")
    (directory / "before.json").write_text(json.dumps(snapshot(fixture)))


class ProcessFixture(fixtures.CommandEntryFixture):
    def __init__(self, directory):
        super().__init__(directory, limit=LIMIT)
        self.crash_phase = None
        self.crash_directory = None
        self.deleted = False
        self.verify_before_intent = False
        self.observed_argv = None
        self.observed_environment = None

    def external(self, request, timeout):
        path, method = urlsplit(request.full_url).path, request.get_method()
        if (
            self.deleted
            and method == "GET"
            and path == f"/repos/owner/repo/issues/comments/{self.SOURCE_ID}"
        ):
            self._meter(request)
            return json_response({"message": "Not Found"}, 404)
        response = super().external(request, timeout)
        selected = record(self)
        action = selected.api_run_accounting["action"] if selected else None
        matched = False
        if action is not None and action["phase"] == "pending":
            if self.crash_phase == "unstarted":
                # This ordinary source read follows the completed admission CAS.
                matched = (
                    method == "GET"
                    and path.endswith(f"/issues/comments/{self.SOURCE_ID}")
                    and not action["execution_started"]
                )
            elif self.crash_phase == "started":
                matched = (
                    method == "GET"
                    and path.endswith("/issues/7/comments")
                    and action["execution_started"]
                    and not selected.operator_paused
                )
            elif self.crash_phase == "pending_receipt":
                # Native business PATCH has landed; no terminal result receipt yet.
                matched = method == "PATCH" and selected.operator_paused
        if matched:
            save(self, self.crash_directory)
            os._exit(77)
        return response

    def invoke_observed(self, *, event):
        original = package_cli.main

        def main(argv):
            if self.verify_before_intent:
                argv = [argv[0], "--verify-source-before-intent", *argv[1:]]
            self.observed_argv = list(argv)
            self.observed_environment = {
                key: os.environ.get(key)
                for key in (
                    "GITHUB_RUN_ID",
                    "GITHUB_RUN_ATTEMPT",
                    "GITHUB_EVENT_NAME",
                    "GITHUB_ACTOR",
                    "GITHUB_TRIGGERING_ACTOR",
                    "REVIEW_SENSEI_WORKFLOW_REF",
                    "REVIEW_SENSEI_WORKFLOW_SHA",
                )
            }
            self.observed_environment["event"] = json.loads(
                Path(os.environ["GITHUB_EVENT_PATH"]).read_text()
            )
            return original(argv)

        with patch.object(package_cli, "main", side_effect=main):
            return self.invoke_command(event_override=event)


def original_event(fixture):
    return {
        "action": "created",
        "issue": {"number": 7, "pull_request": {}},
        "comment": copy.deepcopy(fixture.source),
    }


def produce(directory, phase):
    root = directory / "checkout"
    root.mkdir()
    fixture = ProcessFixture(root)
    fixture.select_command("@reviewsensei review pause")
    event = original_event(fixture)
    (directory / "event.json").write_text(json.dumps(event))
    if phase.startswith("live_"):
        fixture.verify_before_intent = True
        if phase == "live_deleted":
            fixture.deleted = True
        else:
            fixture.source.update(
                body="@reviewsensei review status", updated_at="2026-10-26T01:00:00Z"
            )
    elif phase.startswith("before_"):
        event["action"] = phase.removeprefix("before_")
    elif phase != "terminal":
        fixture.crash_phase, fixture.crash_directory = phase, directory
    code = fixture.invoke_observed(event=event)
    save(fixture, directory)
    (directory / "producer.json").write_text(
        json.dumps({"code": code, "error": fixture.error})
    )
    if phase not in {
        "terminal",
        "before_edited",
        "before_deleted",
        "live_edited",
        "live_deleted",
    }:
        raise AssertionError(("required crash not reached", phase, code, fixture.error))


def consume(directory, mutation):
    saved = json.loads((directory / "remote.json").read_text())
    with tempfile.TemporaryDirectory() as fresh:
        fixture = ProcessFixture(fresh)
        for name in persisted.FIELDS:
            setattr(fixture, name, saved[name])
        fixture.root = fixture.directory = Path(saved["fixture_root"])
        fixture.extra_comments = {
            int(key): value for key, value in fixture.extra_comments.items()
        }
        fixture.past_jobs = {
            int(key): value for key, value in fixture.past_jobs.items()
        }
        fixture.resolved_threads = set(saved["resolved_threads"])
        fixture.attempt = 2
        if mutation == "edited":
            fixture.source.update(
                body="@reviewsensei review status", updated_at="2026-10-26T01:00:00Z"
            )
        elif mutation == "deleted":
            fixture.deleted = True
        elif mutation == "restored":
            fixture.source["updated_at"] = "2026-10-26T01:00:00Z"
        elif mutation == "head":
            fixture.live_head = "9" * 40
        code = fixture.invoke_observed(
            event=json.loads((directory / "event.json").read_text())
        )
        result = snapshot(fixture)
        result.update(code=code, error=fixture.error)
        (directory / "after.json").write_text(json.dumps(result))


class SourceLifecycleProcessMatrixTests(unittest.TestCase):
    def worker(self, directory, mode, value):
        root = Path(__file__).resolve().parents[1]
        env = dict(
            os.environ,
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONPATH=os.pathsep.join(
                str(p)
                for p in (
                    root,
                    root / "src",
                    root / "packages/python/review-sensei-github/src",
                    root / "tests",
                )
            ),
        )
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "tests.test_source_lifecycle_process_matrix",
                mode,
                str(directory),
                value,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
        )

    def assert_accounting(self, state):
        self.assertEqual(state["limit"], LIMIT)
        self.assertEqual(
            sum(
                state[key]
                for key in (
                    "startup",
                    "prior",
                    "confirmed",
                    "uncertain",
                    "reserved",
                    "remaining",
                )
            ),
            LIMIT,
        )

    def test_preintent_edited_and_deleted_events_refuse_before_remote_work(self):
        for kind in ("edited", "deleted"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                directory = Path(directory)
                result = self.worker(directory, "produce", "before_" + kind)
                self.assertEqual(result.returncode, 0, result.stderr)
                before = json.loads((directory / "before.json").read_text())
                outcome = json.loads((directory / "producer.json").read_text())
                self.assertEqual(outcome["code"], 1, outcome["error"])
                self.assertEqual(before["trace"], [])
                self.assertIsNone(before["root"])
                self.assertEqual(before["provider_calls"], 0)
                self.assertEqual(before["environment"]["event"]["action"], kind)

    def test_live_source_mutation_before_intent_uses_real_cli_option(self):
        for kind in ("edited", "deleted"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                directory = Path(directory)
                result = self.worker(directory, "produce", "live_" + kind)
                self.assertEqual(result.returncode, 0, result.stderr)
                before = json.loads((directory / "before.json").read_text())
                outcome = json.loads((directory / "producer.json").read_text())
                self.assertEqual(outcome["code"], 1, outcome["error"])
                self.assert_accounting(before)
                self.assertIsNone(before["root"]["api_run_accounting"]["action"])
                self.assertFalse(before["root"]["operator_paused"])
                self.assertEqual(before["business_writes"], 0)
                self.assertEqual(before["provider_calls"], 0)
                self.assertIn("--verify-source-before-intent", before["argv"])
                self.assertTrue(
                    any(
                        path.endswith("/issues/comments/41")
                        for _, method, path in before["trace"]
                        if method == "GET"
                    )
                )

    def test_cold_supported_and_held_routes_preserve_exact_source_and_funding(self):
        for phase in ("unstarted", "started", "pending_receipt", "terminal"):
            for mutation in ("unchanged", "edited", "deleted", "restored", "head"):
                with (
                    self.subTest(phase=phase, mutation=mutation),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    directory = Path(directory)
                    produced = self.worker(directory, "produce", phase)
                    self.assertEqual(
                        produced.returncode,
                        0 if phase == "terminal" else 77,
                        produced.stderr,
                    )
                    before = json.loads((directory / "before.json").read_text())
                    self.assert_accounting(before)
                    action = before["root"]["api_run_accounting"]["action"]
                    self.assertEqual(
                        action["phase"],
                        "terminal" if phase == "terminal" else "pending",
                    )
                    self.assertEqual(action["execution_started"], phase != "unstarted")
                    self.assertEqual(
                        before["root"]["operator_paused"],
                        phase in {"pending_receipt", "terminal"},
                    )
                    cold = self.worker(directory, "consume", mutation)
                    self.assertEqual(cold.returncode, 0, cold.stderr)
                    after = json.loads((directory / "after.json").read_text())
                    self.assert_accounting(after)
                    next_action = after["root"]["api_run_accounting"]["action"]
                    self.assertEqual(
                        next_action["intent"]["source"], action["intent"]["source"]
                    )
                    self.assertEqual(
                        next_action["intent"]["operation_key"],
                        action["intent"]["operation_key"],
                    )
                    self.assertGreaterEqual(
                        after["prior"] + after["uncertain"], before["uncertain"]
                    )
                    self.assertEqual(after["provider_calls"], 0)
                    self.assertLessEqual(after["remaining"], before["remaining"])
                    self.assertEqual(after["environment"]["GITHUB_RUN_ATTEMPT"], "2")
                    self.assertEqual(after["argv"][0], "command-run")
                    self.assertIn("--github-session-ledger", after["argv"])
                    if phase == "unstarted" and mutation == "unchanged":
                        self.assertEqual(after["code"], 0, after["error"])
                        self.assertTrue(after["root"]["operator_paused"])
                        self.assertEqual(after["business_writes"], 1)
                        self.assertEqual(next_action["phase"], "terminal")
                    else:
                        self.assertEqual(
                            after["business_writes"],
                            before["business_writes"],
                            after["error"],
                        )
                        self.assertEqual(
                            after["root"]["operator_paused"],
                            before["root"]["operator_paused"],
                        )
                        self.assertEqual(next_action["phase"], action["phase"])
                        self.assertEqual(after["code"], 1, after["error"])
                        self.assertNotIn("Traceback", after["error"])
                        if mutation != "head":
                            self.assertIn(
                                "caller run is already terminal"
                                if phase == "terminal"
                                else "hold fail-closed",
                                after["error"],
                            )


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] in {"produce", "consume"}:
        with patch(
            "socket.socket.connect", side_effect=AssertionError("unexpected network")
        ):
            {"produce": produce, "consume": consume}[sys.argv[1]](
                Path(sys.argv[2]), sys.argv[3]
            )
    else:
        unittest.main()
