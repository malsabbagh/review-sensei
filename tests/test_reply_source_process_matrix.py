"""Actual hosted reply-run source lifecycle across interpreter deaths.

Only synthetic remote state and checkout files survive. No cessation or receipt
capability is invented; stopped started work remains an explicit ordinary hold.
"""

import copy
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit

from fake_github_http import json_response
from review_sensei_github import cli
from review_sensei_github.session_ledger import parse_session_comment

from review_sensei.api_work import ApiRunAccounting
from tests import test_action_reply_entry as fixtures

FIELDS = (
    "comment",
    "extra_comments",
    "reviews",
    "inline",
    "threads",
    "check_runs",
    "run_id",
    "owner_job",
    "event_name",
    "policy",
    "policy_ref",
    "live_head",
    "live_base",
    "requested_head",
    "requested_base",
    "source",
    "provider_calls",
    "reply_posts",
    "reactions",
    "removed_reactions",
)


class ProcessFixture(fixtures.ReplyEntryFixture):
    phase = None
    destination = None
    mutation = None

    def selected_record(self):
        return (
            parse_session_comment(self.comment["body"], identity=fixtures.IDENTITY)
            if self.comment
            else None
        )

    def observe(self):
        record = self.selected_record()
        accounting = (
            ApiRunAccounting.from_document(record.api_run_accounting)
            if record
            else None
        )
        return {
            "record": record.to_dict() if record else None,
            "provider_calls": self.provider_calls,
            "reply_posts": self.reply_posts,
            "argv": self.argv,
            "event": self.event,
            "environment": self.environment,
            "comment_order": list(self.extra_comments),
            "budget": (
                {
                    "limit": accounting.configuration.limit,
                    "remaining": accounting.remaining,
                    "confirmed": accounting.confirmed,
                    "uncertain": accounting.uncertain,
                    "reserved": accounting.reserved_undispatched,
                    "prior": accounting.prior_nonstartup_liability,
                    "startup": accounting.startup.liability,
                }
                if accounting
                else None
            ),
        }

    def save(self):
        data = {key: getattr(self, key) for key in FIELDS}
        data["root"] = str(self.root)
        (self.destination / "remote.json").write_text(json.dumps(data))
        (self.destination / "before.json").write_text(json.dumps(self.observe()))

    def external(self, request, timeout):
        path, method = urlsplit(request.full_url).path, request.get_method()
        if (
            method == "GET"
            and path.endswith("/issues/comments/41")
            and self.mutation == "deleted"
        ):
            self.trace.append((self.attempt, method, path))
            return json_response({"message": "Not Found"}, 404)
        if urlsplit(
            request.full_url
        ).hostname == "broker.github.test" and self.mutation in {
            "edited",
            "restored",
            "deleted",
        }:
            # The existing fixture assumes event == current source. Model the
            # actual broker's rejection, not an attestation for stale source.
            self.trace.append((self.attempt, method, path))
            raise HTTPError(
                request.full_url,
                403,
                "Forbidden",
                {},
                io.BytesIO(b'{"error":"broker_session_actor_rejected"}'),
            )
        response = super().external(request, timeout)
        record = self.selected_record()
        action = record.api_run_accounting["action"] if record else None
        crash = False
        if action and action["phase"] == "pending":
            if self.phase == "unstarted":
                crash = method == "PATCH" and not action["execution_started"]
            elif self.phase == "started":
                crash = (
                    action["execution_started"]
                    and method == "GET"
                    and path.endswith("/issues/7/comments")
                    and not self.reply_posts
                )
            elif self.phase == "landed":
                crash = (
                    method == "POST"
                    and path.endswith("/issues/7/comments")
                    and self.reply_posts == 1
                )
        if crash:
            self.save()
            os._exit(77)
        return response

    def invoke(self, original_event=None):
        main = cli.main

        def observed(argv):
            path = Path(os.environ["GITHUB_EVENT_PATH"])
            if original_event is not None:
                path.write_text(json.dumps(original_event))
                # Preserve the original invocation source revision, not a new event.
                argv[argv.index("--source-updated-at") + 1] = original_event["comment"][
                    "updated_at"
                ]
            self.argv = list(argv)
            self.event = json.loads(path.read_text())
            self.environment = {
                key: os.environ[key]
                for key in (
                    "GITHUB_RUN_ID",
                    "GITHUB_RUN_ATTEMPT",
                    "GITHUB_EVENT_NAME",
                    "GITHUB_ACTOR",
                    "REVIEW_SENSEI_WORKFLOW_SHA",
                )
            }
            return main(argv)

        with patch.object(cli, "main", side_effect=observed):
            return self.invoke_reply()


def worker(directory, phase, mutation):
    if mutation == "produce":
        root = directory / "checkout"
        root.mkdir()
        fixture = ProcessFixture(root)
        unrelated = copy.deepcopy(fixture.source)
        unrelated.update(id=42, body="Unrelated discussion, not a review request.")
        fixture.extra_comments[42] = unrelated
        fixture.destination, fixture.phase = directory, phase
        original_event = None
        if phase.startswith("before_"):
            original_event = {
                "issue": {"number": 7},
                "comment": copy.deepcopy(fixture.source),
                "repository": {"id": 123, "full_name": "owner/repo"},
                "sender": fixture.source["user"],
            }
            fixture.mutation = phase.removeprefix("before_")
            if fixture.mutation == "edited":
                fixture.source.update(
                    body="@reviewsensei Changed question",
                    updated_at="2026-10-27T00:00:00Z",
                )
        code = fixture.invoke(original_event)
        fixture.save()
        (directory / "result.json").write_text(
            json.dumps({"code": code, "error": fixture.error})
        )
        if phase != "terminal" and not phase.startswith("before_"):
            raise AssertionError(("crash not reached", phase, code, fixture.error))
    else:
        saved = json.loads((directory / "remote.json").read_text())
        original = json.loads((directory / "before.json").read_text())
        with tempfile.TemporaryDirectory() as fresh:
            fixture = ProcessFixture(fresh)
            for key in FIELDS:
                setattr(fixture, key, saved[key])
            fixture.root = fixture.directory = Path(saved["root"])
            fixture.extra_comments = {
                int(k): v for k, v in fixture.extra_comments.items()
            }
            fixture.attempt = 2
            fixture.mutation = mutation
            if mutation == "edited":
                fixture.source = copy.deepcopy(fixture.source)
                fixture.source.update(
                    body="@reviewsensei A different question",
                    updated_at="2026-10-27T00:00:00Z",
                )
                fixture.extra_comments[41] = fixture.source
            elif mutation == "restored":
                fixture.source["updated_at"] = "2026-10-27T00:00:00Z"
                fixture.extra_comments[41] = fixture.source
            elif mutation == "reordered":
                fixture.extra_comments = dict(
                    reversed(list(fixture.extra_comments.items()))
                )
            elif mutation == "head":
                fixture.live_head = "9" * 40
            code = fixture.invoke(original["event"])
            result = fixture.observe()
            result.update(code=code, error=fixture.error)
            (directory / "after.json").write_text(json.dumps(result))


class ReplySourceProcessTests(unittest.TestCase):
    def run_worker(self, directory, phase, mutation):
        root = Path(__file__).resolve().parents[1]
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "tests.test_reply_source_process_matrix",
                str(directory),
                phase,
                mutation,
            ],
            cwd=root,
            env={
                **os.environ,
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": os.pathsep.join(
                    str(root / p)
                    for p in (
                        ".",
                        "src",
                        "packages/python/review-sensei-github/src",
                        "tests",
                    )
                ),
            },
            capture_output=True,
            text=True,
            timeout=90,
        )

    def test_before_intent_source_rejection_has_no_business_effect(self):
        for mutation in ("edited", "deleted"):
            with (
                self.subTest(mutation=mutation),
                tempfile.TemporaryDirectory() as temporary,
            ):
                directory = Path(temporary)
                process = self.run_worker(directory, "before_" + mutation, "produce")
                self.assertEqual(process.returncode, 0, process.stderr)
                before = json.loads((directory / "before.json").read_text())
                result = json.loads((directory / "result.json").read_text())
                self.assertEqual(result["code"], 1, result["error"])
                self.assertIn("Broker token request was rejected", result["error"])
                self.assertIsNone(before["record"])
                self.assertEqual(before["provider_calls"], 0)
                self.assertEqual(before["reply_posts"], 0)

    def test_reply_source_cold_matrix(self):
        for phase in ("unstarted", "started", "landed", "terminal"):
            for mutation in (
                "unchanged",
                "edited",
                "deleted",
                "restored",
                "reordered",
                "head",
            ):
                with (
                    self.subTest(phase=phase, mutation=mutation),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    directory = Path(temporary)
                    producer = self.run_worker(directory, phase, "produce")
                    self.assertEqual(
                        producer.returncode,
                        0 if phase == "terminal" else 77,
                        producer.stderr,
                    )
                    before = json.loads((directory / "before.json").read_text())
                    consumer = self.run_worker(directory, phase, mutation)
                    self.assertEqual(consumer.returncode, 0, consumer.stderr)
                    after = json.loads((directory / "after.json").read_text())
                    positive = phase == "unstarted" and mutation in {
                        "unchanged",
                        "reordered",
                    }
                    self.assertEqual(
                        after["code"], 0 if positive else 1, after["error"]
                    )
                    self.assertNotIn("Traceback", after["error"])
                    self.assertEqual(
                        after["provider_calls"],
                        before["provider_calls"] + int(positive),
                    )
                    self.assertEqual(
                        after["reply_posts"], before["reply_posts"] + int(positive)
                    )
                    self.assertEqual(after["event"], before["event"])
                    self.assertEqual(after["environment"]["GITHUB_RUN_ATTEMPT"], "2")
                    self.assertEqual(before["argv"][0], "reply-run")
                    if phase == "terminal":
                        produced = json.loads((directory / "result.json").read_text())
                        self.assertEqual(produced["code"], 0, produced["error"])
                        self.assertEqual(before["reply_posts"], 1)
                    if mutation == "reordered":
                        self.assertGreaterEqual(len(before["comment_order"]), 2)
                        # Publication may append a reply, but the original two
                        # source rows really change order on the cold page.
                        self.assertLess(
                            after["comment_order"].index(42),
                            after["comment_order"].index(41),
                        )
                    old = before["record"]["api_run_accounting"]["action"]
                    new = after["record"]["api_run_accounting"]["action"]
                    self.assertEqual(
                        new["intent"]["operation_key"], old["intent"]["operation_key"]
                    )
                    self.assertEqual(new["intent"]["source"], old["intent"]["source"])
                    self.assertEqual(
                        new["phase"], "terminal" if positive else old["phase"]
                    )
                    if mutation in {"edited", "deleted", "restored"}:
                        self.assertIn(
                            "Broker token request was rejected", after["error"]
                        )
                    elif phase == "terminal" and mutation != "head":
                        self.assertIn("caller run is already terminal", after["error"])
                    elif not positive and mutation != "head":
                        self.assertIn("hold fail-closed", after["error"])
                    self.assertGreaterEqual(
                        after["budget"]["prior"] + after["budget"]["uncertain"],
                        before["budget"]["uncertain"],
                    )
                    for observed in (before, after):
                        budget = observed["budget"]
                        self.assertEqual(budget["limit"], 512)
                        self.assertEqual(
                            sum(v for k, v in budget.items() if k != "limit"), 512
                        )
                    self.assertLessEqual(
                        after["budget"]["remaining"], before["budget"]["remaining"]
                    )


if __name__ == "__main__":
    with patch.object(
        socket.socket, "connect", side_effect=AssertionError("unexpected network")
    ):
        worker(Path(sys.argv[1]), sys.argv[2], sys.argv[3])
