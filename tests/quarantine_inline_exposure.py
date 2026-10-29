"""Opt-in old-installed inline dispatch proof; every external transport is fake.

The fixture changes only the original event/CLI inputs and the fake GitHub and
broker boundaries. Actual installed reply-run, authority, journals, mutation
poisoning, publication, accounting and finally cleanup execute unchanged.
"""

import copy
import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from fake_github_http import json_response
from review_sensei_github import cli as package_cli
from review_sensei_github.publication import approval_eligibility_from_body
from review_sensei_github.run_authority import parse_authority
from test_action_reply_entry import GRANT, ReplyEntryFixture
from test_review_run import EntryFixture

RECORDS = []


class InlineExposureFixture(ReplyEntryFixture):
    def broker_response(self, payload):
        granted = "session_grant" in payload
        authority = parse_authority(payload["session_attestation"], grant=granted)
        assert authority["purpose"] == "reply"
        assert authority["source"] == {
            "kind": "review_comment",
            "id": self.source["id"],
            "updated_at": self.source["updated_at"],
            "body_sha256": hashlib.sha256(self.source["body"].encode()).hexdigest(),
        }
        if granted:
            assert payload["session_grant"] == GRANT
            return {
                "session_attestation": authority,
                "control_receipt": {**payload["control_budget"], "dispatched": 1},
            }
        result = {
            "token": "synthetic-" + payload["capability"],
            "capability": payload["capability"],
            "control_receipt": {**payload["control_budget"], "dispatched": 1},
        }
        if payload["capability"] == "review_session":
            result.update(
                session_state="known",
                app_id=999,
                reservation_owner={
                    "run_id": str(self.run_id),
                    "head_sha": self.live_head,
                },
                session_grant=GRANT,
                session_attestation={
                    **authority,
                    "actor": {"login": "alice", "type": "User", "association": "OWNER"},
                },
            )
        return result

    def invoke_reply(self):
        original = package_cli.main

        def inline_entry(arguments):
            values = list(arguments)
            values[values.index("--source-kind") + 1] = "inline"
            values.extend(("--root-comment-id", str(self.source["in_reply_to_id"])))
            return original(values)

        with patch.object(package_cli, "main", inline_entry):
            return super().invoke_reply()

    def external(self, request, timeout):
        method, path = request.get_method(), urlsplit(request.full_url).path
        if "/reactions" in path:
            self.trace.append((self.attempt, method, path))
            if method == "POST":
                item = {
                    "id": 800 + len(self.reactions),
                    "content": "eyes",
                    "user": {"id": 999, "type": "Bot"},
                }
                self.reactions.append(item)
                return json_response(item, 201)
            self.removed_reactions.append(int(path.rsplit("/", 1)[1]))
            return json_response({}, 204)
        if path == f"/repos/owner/repo/pulls/comments/{self.source['id']}":
            self.trace.append((self.attempt, method, path))
            return json_response(copy.deepcopy(self.source))
        if (
            method == "POST"
            and path
            == f"/repos/owner/repo/pulls/7/comments/{self.source['in_reply_to_id']}/replies"
        ):
            self.trace.append((self.attempt, method, path))
            body = json.loads(request.data)
            item = {
                **copy.deepcopy(self.source),
                "id": 9_100_000 + self.reply_posts,
                "body": body["body"],
                "user": {"id": 999, "login": "reviewsensei[bot]", "type": "Bot"},
            }
            self.reply_posts += 1
            self.inline.append(item)
            return json_response(item, 201)
        return super().external(request, timeout)


class TracedInlineExposureFixture(InlineExposureFixture):
    def configure_trace(
        self, *, cancel_at=None, unknown_at=None, stored=False, foreign_archive=False
    ):
        self.dispatches, self.delayed = [], []
        self.cancel_at, self.unknown_at, self.stored, self.foreign_archive = (
            cancel_at,
            unknown_at,
            stored,
            foreign_archive,
        )
        self.archive_delayed = False

    def external(self, request, timeout):
        method, path = request.get_method(), urlsplit(request.full_url).path
        payload = request.data or b""
        body = json.loads(payload) if payload else {}
        mutating = (
            path.startswith("/repos/")
            and method in {"POST", "PATCH", "PUT", "DELETE"}
            or path == "/graphql"
            and body.get("operationName") == "ResolveReviewThread"
        )
        row = {
            "index": len(self.dispatches) + 1,
            "method": method,
            "path": path,
            "body_bytes": len(payload),
            "body_sha256": hashlib.sha256(payload).hexdigest(),
            "mutation": mutating,
            "inline_root": self.source["in_reply_to_id"],
        }
        self.dispatches.append(row)
        index = len(self.dispatches)
        if self.cancel_at == index:
            self.trace.append((self.attempt, method, path))
            raise KeyboardInterrupt()
        startup_archive = (
            self.foreign_archive
            and not self.archive_delayed
            and method == "POST"
            and path.endswith("/issues/7/comments")
            and str(body.get("body", "")).startswith(
                "ReviewSensei immutable evidence part"
            )
        )
        if startup_archive:
            self.archive_delayed = True
            self.extra_comments[7_900_000] = {
                "id": 7_900_000,
                "body": body["body"],
                "user": {"id": 999, "login": "reviewsensei[bot]", "type": "Bot"},
                "issue_url": "https://api.github.test/repos/owner/repo/issues/7",
            }
        if startup_archive or self.unknown_at == index:
            if self.stored and not startup_archive:
                super().external(request, timeout)
            else:
                self.trace.append((self.attempt, method, path))
                self.delayed.append(row)
            raise TimeoutError("synthetic delayed physical dispatch; never retried")
        return super().external(request, timeout)


def inline_fixture(directory, *, human=False, **trace):
    initial = EntryFixture(directory)
    initial.provider_comments = (
        [
            {
                "path": "src/app.py",
                "line": 2,
                "side": "RIGHT",
                "body": "Confirm the caller contract against the added change.",
                "severity": "high",
                "blocking": False,
                "needs_human": True,
            }
        ]
        if human
        else []
    )
    assert initial.invoke(human=human) == 0, initial.error
    fixture = TracedInlineExposureFixture.__new__(TracedInlineExposureFixture)
    fixture.__dict__.update(initial.__dict__)
    original_head = fixture.live_head
    fixture.next_run()
    fixture.live_head = fixture.requested_head = original_head
    fixture.prepare_reply()
    fixture.event_name = "pull_request_review_comment"
    if not human:
        # A legacy optional App thread outside the current human inventory is
        # still a valid ordinary inline reply target. It grants no decision.
        fixture.inline.append(
            {
                "id": 200,
                "body": "An earlier optional observation about the changed branch.",
                "path": "src/app.py",
                "line": 2,
                "commit_id": original_head,
                "user": {"id": 999, "login": "reviewsensei[bot]", "type": "Bot"},
                "pull_request_url": "https://api.github.test/repos/owner/repo/pulls/7",
            }
        )
    root = fixture.inline[0]
    fixture.source.update(
        {
            "pull_request_url": "https://api.github.test/repos/owner/repo/pulls/7",
            "in_reply_to_id": root["id"],
            "path": root["path"],
            "line": root["line"],
            "commit_id": original_head,
        }
    )
    fixture.inline.append(fixture.source)
    fixture.thread_rows = [
        {
            "id": "native-thread-" + str(root["id"]),
            "isResolved": False,
            "comments": {
                "nodes": [
                    {
                        "databaseId": root["id"],
                        "body": root["body"],
                        "path": root["path"],
                        "line": root["line"],
                        "author": {"login": "reviewsensei", "__typename": "Bot"},
                    }
                ]
            },
        }
    ]
    if human:
        eligibility = approval_eligibility_from_body(fixture.reviews[-1]["body"])
        fingerprint = eligibility.human_review.findings[0].fingerprint
        fixture.source["body"] = (
            "@reviewsensei The caller contract is intentional; the added change is checked."
        )
        fixture.reply_document = {
            "body": "The explanation is supported by the current diff.",
            "assessments": [
                {
                    "fingerprint": fingerprint,
                    "decision": "dismissed",
                    "rationale": "The current diff contains the intended added change.",
                    "human_evidence": "The caller contract is intentional",
                    "diff_evidence": "@@ -1 +1,2 @@\n keep\n+change",
                }
            ],
        }
    fixture.configure_trace(**trace)
    return fixture


class InlineExposureQualification(unittest.TestCase):
    def test_actual_inline_ordinary_and_human_routes_reach_native_reply_without_broader_review(
        self,
    ):
        for human in (False, True):
            with self.subTest(human=human), tempfile.TemporaryDirectory() as directory:
                fixture = inline_fixture(directory, human=human)
                self.assertEqual(fixture.invoke_reply(), 0, fixture.error)
                self.assertGreater(fixture.reply_posts, 0)
                self.assertFalse(fixture.enable_broader_review)
                RECORDS.append(
                    {
                        "route": "human-inline" if human else "ordinary-inline",
                        "dispatches": fixture.dispatches,
                        "outcome": 0,
                    }
                )

    def test_foreign_archive_then_each_later_unknown_allocation_has_at_most_two_delayed_carriers(
        self,
    ):
        for human in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                baseline = inline_fixture(directory, human=human, foreign_archive=True)
                self.assertEqual(baseline.invoke_reply(), 0, baseline.error)
                self.assertEqual(len(baseline.delayed), 1)
                later = [
                    r["index"]
                    for r in baseline.dispatches
                    if r["method"] == "POST"
                    and r["path"].endswith("/issues/7/comments")
                ][1:]
                self.assertTrue(later)
            for index in later:
                for stored in (False, True):
                    with (
                        self.subTest(human=human, index=index, stored=stored),
                        tempfile.TemporaryDirectory() as directory,
                    ):
                        fixture = inline_fixture(
                            directory,
                            human=human,
                            foreign_archive=True,
                            unknown_at=index,
                            stored=stored,
                        )
                        result = fixture.invoke_reply()
                        self.assertLessEqual(len(fixture.delayed), 1 if stored else 2)
                        allocations = [
                            r
                            for r in fixture.dispatches
                            if r["method"] == "POST"
                            and r["path"].endswith("/issues/7/comments")
                        ]
                        if not stored:
                            self.assertFalse(
                                any(r["index"] > index for r in allocations)
                            )
                        attempts = [
                            r for r in fixture.dispatches if r["index"] == index
                        ]
                        self.assertEqual(len(attempts), 1)
                        RECORDS.append(
                            {
                                "route": "human-inline" if human else "ordinary-inline",
                                "fault": "stored" if stored else "delayed",
                                "fault_index": index,
                                "outcome": result,
                                "dispatches": fixture.dispatches,
                                "delayed": fixture.delayed,
                            }
                        )

    def test_cancellation_at_every_physical_boundary_retains_only_fixed_old_cleanup_targets(
        self,
    ):
        for human in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                baseline = inline_fixture(directory, human=human, foreign_archive=True)
                self.assertEqual(baseline.invoke_reply(), 0, baseline.error)
                count = len(baseline.dispatches)
            for index in range(1, count + 1):
                with (
                    self.subTest(human=human, index=index),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    fixture = inline_fixture(
                        directory, human=human, foreign_archive=True, cancel_at=index
                    )
                    result = fixture.invoke_reply()
                    self.assertNotEqual(result, 0)
                    # Finally cleanup can delete this execution's own known
                    # reaction. It cannot allocate a new carrier or root.
                    after = fixture.dispatches[index:]
                    self.assertTrue(
                        all(
                            r["method"] == "DELETE" and "/reactions/" in r["path"]
                            for r in after
                        ),
                        after,
                    )
                    self.assertLessEqual(len(fixture.delayed), 1)
                    RECORDS.append(
                        {
                            "route": "human-inline" if human else "ordinary-inline",
                            "fault": "cancel",
                            "fault_index": index,
                            "outcome": result,
                            "dispatches": fixture.dispatches,
                            "delayed": fixture.delayed,
                        }
                    )


if __name__ == "__main__":
    unittest.main()
