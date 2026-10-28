"""Public CLI quarantine → empty fresh review → individual decisions → verify."""

import copy
import itertools
import json
import os
import random
import string
import subprocess
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from fake_github_http import json_response
from review_sensei_github.publication import approval_eligibility_from_body
from review_sensei_github.session_ledger import parse_session_comment
from test_action_run import CommandEntryFixture
from test_quarantine_activation import event, registered_resource
from test_quarantine_creation import CreationFixture
from test_quarantine_evidence import git, original_objects
from test_review_run import EntryFixture

_RUNS = itertools.count(2900)


class JourneyFixture(CreationFixture, CommandEntryFixture):
    def __init__(self, directory):
        super().__init__(directory, limit=512)
        self.creation_run = None
        self.creation_metadata = None
        self.completed_attempts = {}
        self.allow_publication = True

    def external(self, request, timeout):
        path, method = urlsplit(request.full_url).path, request.get_method()
        if method == "POST" and path == "/graphql":
            payload = json.loads(request.data)
            from review_sensei_github.quarantine_census import QUERY

            if payload.get("query") == QUERY:
                import base64

                self._meter(request)
                assert self.accounted.observations[-1]["mutation"] is False
                variables = payload["variables"]
                rows = ([self.comment] if self.comment else []) + list(
                    self.extra_comments.values()
                )
                after = variables["after"]
                offset = (
                    int(base64.b64decode(after).decode().split(":")[1]) if after else 0
                )
                selected = rows[offset : offset + variables["first"]]
                cursor = (
                    base64.b64encode(
                        f"position:{offset + len(selected)}".encode()
                    ).decode()
                    if selected
                    else None
                )
                subject = {
                    "number": 7,
                    "repository": {"databaseId": 123, "nameWithOwner": "owner/repo"},
                }
                return json_response(
                    {
                        "data": {
                            "repository": {
                                "databaseId": 123,
                                "nameWithOwner": "owner/repo",
                                "pullRequest": {
                                    "number": 7,
                                    "comments": {
                                        "totalCount": len(rows),
                                        "pageInfo": {
                                            "hasNextPage": offset + len(selected)
                                            < len(rows),
                                            "endCursor": cursor,
                                        },
                                        "nodes": [
                                            {
                                                "fullDatabaseId": str(item["id"]),
                                                "body": item["body"],
                                                "createdAt": item.get(
                                                    "created_at", "2026-10-28T00:00:00Z"
                                                ),
                                                "updatedAt": item.get(
                                                    "updated_at", "2026-10-28T00:00:00Z"
                                                ),
                                                "author": None
                                                if item.get("user") is None
                                                else {
                                                    "__typename": item["user"]["type"],
                                                    "login": item["user"]["login"],
                                                    "databaseId": item["user"]["id"],
                                                },
                                                "pullRequest": subject
                                                if item.get(
                                                    "issue_url",
                                                    "https://api.github.test/repos/owner/repo/issues/7",
                                                )
                                                == "https://api.github.test/repos/owner/repo/issues/7"
                                                else {**subject, "number": 8},
                                            }
                                            for item in selected
                                        ],
                                    },
                                },
                            }
                        }
                    }
                )
        if (
            self.creation_run is not None
            and self.attempt == 2
            and (
                path == f"/repos/owner/repo/actions/runs/{self.creation_run}"
                or path.startswith(
                    f"/repos/owner/repo/actions/runs/{self.creation_run}/attempts/"
                )
                and path.endswith("/jobs")
            )
        ):
            return EntryFixture.external(self, request, timeout)
        if method == "GET" and path.startswith("/repos/owner/repo/pulls/7/reviews/"):
            self._meter(request)
            identifier = int(path.rsplit("/", 1)[-1])
            found = [review for review in self.reviews if review["id"] == identifier]
            return json_response(
                copy.deepcopy(found[0]) if found else {"message": "Not Found"},
                200 if found else 404,
            )
        if self.command_active:
            if path.endswith("/collaborators/alice/permission"):
                self._meter(request)
                return json_response(
                    {
                        "permission": self.role,
                        "user": {"id": 42, "login": "alice", "type": "User"},
                    }
                )
            if (
                urlsplit(request.full_url).hostname == "broker.github.test"
                or path.endswith(f"/issues/comments/{self.SOURCE_ID}")
                or method == "PUT"
                or path == "/graphql"
            ):
                return CommandEntryFixture.external(self, request, timeout)
        if (
            self.creation_run is not None
            and path == f"/repos/owner/repo/actions/runs/{self.creation_run}/attempts/1"
        ):
            self._meter(request)
            return json_response(
                {
                    **self.creation_metadata,
                    "status": "completed"
                    if self.run_id != self.creation_run
                    else "in_progress",
                }
            )
        if (
            self.creation_run is not None
            and self.run_id != self.creation_run
            and "/actions/runs/" in path
            and str(self.source_previous.api_run_accounting["identity"]["run_id"])
            not in path
        ):
            response = CommandEntryFixture.external(self, request, timeout)
            if path == f"/repos/owner/repo/actions/runs/{self.creation_run}":
                value = json.loads(response.read(524288))
                value.update(self.creation_metadata)
                value["status"] = "completed"
                value["run_attempt"] = self.completed_attempts.get(self.creation_run, 1)
                return json_response(value)
            return response
        if (
            self.allow_publication
            and method == "POST"
            and path.endswith("/pulls/7/reviews")
        ):
            return EntryFixture.external(self, request, timeout)
        response = super().external(request, timeout)
        if self.comment is not None:
            self.comment.setdefault(
                "issue_url", "https://api.github.test/repos/owner/repo/issues/7"
            )
        return response

    def next_run(self):
        self.completed_attempts[self.run_id] = self.attempt
        super().next_run()

    def revision(self, previous, *, binary=False, remove_binary=False):
        git(self.root, "checkout", "--detach", previous)
        if binary:
            (self.root / "asset.png").write_bytes(b"\x00\x01\x02")
        if remove_binary:
            (self.root / "asset.png").unlink()
            (self.root / "src/app.py").write_text("keep\nchange\nmore\n")
        elif not binary:
            (self.root / "src/app.py").write_text("keep\nchange\nmore\nfresh\n")
        git(
            self.root,
            "add",
            "--all",
            "--",
            "src/app.py",
            *(["asset.png"] if binary or remove_binary else []),
        )
        git(self.root, "commit", "-m", "synthetic source revision")
        head = git(self.root, "rev-parse", "HEAD")
        diff = subprocess.check_output(
            ["git", "-C", str(self.root), "diff", self.live_base + "..." + head]
        ).decode()
        git(self.root, "checkout", "--detach", self.policy)
        self.live_head = self.requested_head = head
        (self.root / "review.patch").write_text(diff, encoding="utf-8", newline="\n")
        return head

    def original_origins(self, *, failed_count=1, published_count=1):
        original_objects(self)
        first_head = self.revision(self.live_head, binary=True)
        self.provider_comments = [
            {
                "path": "src/app.py",
                "line": 2,
                "side": "RIGHT",
                "body": f"Original published human assessment {number} needs external source validation.",
                "severity": "high",
                "blocking": False,
                "needs_human": True,
            }
            for number in range(published_count)
        ]
        self.assert_result = self.invoke(human=True)
        first = parse_session_comment(
            self.comment["body"], identity=self.selected.identity
        )
        assert self.assert_result == 0, self.error
        assert first.completed_initial_reviews == 0, first.completed_initial_reviews
        self.next_run()
        self.revision(first_head, remove_binary=True)
        self.allow_publication = False
        self.provider_comments = [
            {
                "path": "*" if number < 29 else "src/app.py",
                "line": None if number < 29 else 2,
                "side": "FILE" if number < 29 else "RIGHT",
                "body": f"Original retained assessment {number} requires exact source validation.",
                "severity": "high" if number < 29 else "low",
                "blocking": number < 29,
            }
            for number in range(failed_count)
        ]
        if failed_count == 80:
            # Realistic incompressible finding detail forces the same five
            # immutable failed-source carriers as the retained incident value.
            random_source = random.Random(387)
            for comment in self.provider_comments:
                comment["body"] += " " + "".join(
                    random_source.choices(string.ascii_letters + string.digits, k=1800)
                )
        assert self.invoke() == 1, self.error
        old = parse_session_comment(
            self.comment["body"], identity=self.selected.identity
        )
        assert old.transaction.phase == "publication_failed"
        assert old.completed_initial_reviews == 1
        self.source_previous = old
        old_head = old.transaction.head_sha
        self.next_run()
        self.run_id = self.creation_run = next(_RUNS)
        self.revision(old_head)
        self.event_name = "workflow_dispatch"
        self.quarantine_predecessor_comment_id = 1_000_000
        registration = {
            "schema_version": "quarantine-activation-v1",
            "repository": "owner/repo",
            "repository_id": 123,
            "pull_request": 7,
            "predecessor_comment_id": 1_000_000,
            "predecessor_record_sha256": old.record_sha256,
            "transaction_id": old.transaction.transaction_id,
            "head_sha": self.live_head,
            "base_sha": self.live_base,
            "workflow_id": self.run_id + 100,
            "workflow_path": ".github/workflows/review-sensei-quarantine.yml",
            "workflow_blob": "f" * 40,
            "run_number": 1,
        }
        self.creation_metadata = self.creator()
        self.event_document = event(registration)
        self.provider_comments = []
        self.allow_publication = True
        return old, registration

    def current_record(self):
        roots = [
            parse_session_comment(item["body"], identity=self.selected.identity)
            for item in self.extra_comments.values()
            if "reviewsensei:session:v2" in item.get("body", "")
        ]
        assert len(roots) == 1
        return roots[0]


class QuarantineJourneyTests(unittest.TestCase):
    def test_empty_fresh_review_cannot_clear_both_origins_and_only_exact_decisions_allow_approval(
        self,
    ):
        self.exercise_counts(1, 1)

    def test_all_80_raw_findings_and_52_independent_obligations_complete_the_safe_journey(
        self,
    ):
        self.exercise_counts(80, 23)

    def test_delayed_old_root_and_old_head_check_cannot_overwrite_the_successor(self):
        self.exercise_counts(1, 1, delayed_old=True)

    def exercise_counts(
        self,
        failed_count,
        published_count,
        *,
        delayed_old=False,
        fixture_type=JourneyFixture,
    ):
        with tempfile.TemporaryDirectory() as directory:
            fixture = fixture_type(directory)
            if hasattr(fixture, "bridge"):
                self.addCleanup(fixture.bridge.close)
            old, registration = fixture.original_origins(
                failed_count=failed_count, published_count=published_count
            )
            old_body = fixture.comment["body"]
            metrics = []

            def measure(label):
                from review_sensei.api_work import ApiRunAccounting

                state = ApiRunAccounting.from_document(
                    fixture.current_record().api_run_accounting
                )
                self.assertEqual(state.configuration.limit, 512)
                self.assertGreaterEqual(state.remaining, 0)
                self.assertEqual(
                    len(fixture.accounted.observations), len(fixture.trace)
                )
                metrics.append(
                    {
                        "label": label,
                        "physical_caller_requests": len(fixture.trace),
                        "confirmed": state.confirmed,
                        "uncertain": state.uncertain,
                        "remaining": state.remaining,
                    }
                )

            before_calls = fixture.provider_calls
            with registered_resource([registration]):
                self.assertEqual(
                    fixture.invoke(),
                    0,
                    fixture.error
                    + "\n"
                    + (fixture.root / "publication.json").read_text()
                    if (fixture.root / "publication.json").exists()
                    else fixture.error,
                )
                self.assertEqual(fixture.provider_calls, before_calls + 1)
                measure("quarantine-and-empty-full-review")
                current = fixture.current_record()
                self.assertEqual(current.quarantine_lineage["origin_count"], 2)
                self.assertEqual(
                    current.completed_initial_reviews, old.completed_initial_reviews
                )
                self.assertEqual(fixture.comment["body"], old_body)
                view = approval_eligibility_from_body(
                    fixture.reviews[-1]["body"]
                ).quarantine_assessments
                self.assertEqual(
                    len(view["pending"]), min(failed_count, 29) + published_count
                )
                self.assertEqual(
                    approval_eligibility_from_body(
                        fixture.reviews[-1]["body"]
                    ).facts.review_status,
                    "complete",
                    (fixture.root / "review.json").read_text(),
                )
                self.assertFalse(
                    any(review.get("state") == "APPROVED" for review in fixture.reviews)
                )
                self.assertEqual(
                    fixture.check_runs[-1]["conclusion"], "action_required"
                )
                if delayed_old:
                    from review_sensei_github.session_ledger import (
                        render_session_comment,
                    )

                    successor_body = next(
                        item["body"]
                        for item in fixture.extra_comments.values()
                        if "reviewsensei:session:v2" in item.get("body", "")
                    )
                    # The synthetic GitHub service applies already-dispatched old
                    # effects. Their finite old targets are independently proved
                    # by the actual executor-source isolation contract.
                    old_body = render_session_comment(
                        repository_id=123,
                        pull_request=7,
                        record=old.evolve(generation=old.generation + 1),
                    )
                    fixture.comment["body"] = old_body
                    old_check = copy.deepcopy(fixture.check_runs[-1])
                    old_check.update(
                        id=9_000_000,
                        head_sha=old.transaction.head_sha,
                        conclusion="failure",
                    )
                    fixture.check_runs.append(old_check)
                    self.assertEqual(
                        next(
                            item["body"]
                            for item in fixture.extra_comments.values()
                            if "reviewsensei:session:v2" in item.get("body", "")
                        ),
                        successor_body,
                    )
                for number, pending in enumerate(view["pending"]):
                    fixture.quarantine_predecessor_comment_id = None
                    fixture.select_command(
                        "@reviewsensei dismiss "
                        + pending["instance"]
                        + ' --reason "quarantine:'
                        + pending["origin_scope_sha256"]
                        + ' Original source and caller evidence disproves this exact historical concern."',
                        new_run=True,
                    )
                    self.assertEqual(
                        fixture.invoke_command(),
                        0,
                        f"decision {number + 1}: "
                        + (fixture.error or str(fixture.exception)),
                    )
                    self.assertEqual(fixture.comment["body"], old_body)
                    measure(f"individual-decision-{number + 1}")
                self.assertEqual(
                    sum(
                        review.get("state") == "APPROVED" for review in fixture.reviews
                    ),
                    1,
                    fixture.stdout
                    + "\n"
                    + repr(fixture.check_runs)
                    + "\n"
                    + repr(approval_eligibility_from_body(fixture.reviews[-1]["body"])),
                )
                fixture.select_command("@reviewsensei verify", new_run=True)
                self.assertEqual(
                    fixture.invoke_command(), 0, fixture.error or str(fixture.exception)
                )
                self.assertEqual(
                    sum(
                        review.get("state") == "APPROVED" for review in fixture.reviews
                    ),
                    1,
                )
                measure("verify")
                parts = [
                    item
                    for item in fixture.extra_comments.values()
                    if item.get("body", "").endswith(
                        "<!-- reviewsensei:evidence-part:v1 -->"
                    )
                ]
                from review_sensei.bounded_evidence import (
                    MAX_STORED_PART_BYTES,
                    MAX_STORED_PARTS,
                )

                wire_bytes = sum(len(item["body"].encode()) for item in parts)
                self.assertLessEqual(len(parts), MAX_STORED_PARTS)
                self.assertLessEqual(wire_bytes, MAX_STORED_PART_BYTES)
                destination = os.environ.get("REVIEWSENSEI_QUARANTINE_METRICS")
                if destination:
                    target = Path(destination)
                    target = target.with_name(
                        target.stem
                        + f"-{failed_count}-{published_count}-{delayed_old}"
                        + target.suffix
                    )
                    target.write_text(
                        json.dumps(
                            {
                                "failed_raw_findings": failed_count,
                                "obligations": min(failed_count, 29) + published_count,
                                "runs": metrics,
                                "retained_evidence_parts": len(parts),
                                "retained_evidence_wire_bytes": wire_bytes,
                                "all_comment_body_bytes": sum(
                                    len(item["body"].encode())
                                    for item in fixture.extra_comments.values()
                                ),
                            },
                            indent=2,
                        )
                        + "\n"
                    )
                self.assertIsNone(old.api_run_accounting["terminal_transaction"])
                self.assertEqual(fixture.comment["body"], old_body)


class QuarantineConsumerFaultTests(unittest.TestCase):
    def test_registered_corrupt_duplicate_foreign_and_unsupported_roots_refuse_before_effects(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            fixture = JourneyFixture(directory)
            old, registration = fixture.original_origins()
            with registered_resource([registration]):
                self.assertEqual(fixture.invoke(), 0, fixture.error)
                preserved = copy.deepcopy(fixture.extra_comments)
                native = next(
                    item
                    for item in preserved.values()
                    if "reviewsensei:session:v2" in item.get("body", "")
                )
                origin_id = int(
                    fixture.current_record().quarantine_lineage["origin_manifest"][
                        "parts"
                    ][0]["storage_id"]
                )
                for case in (
                    "missing-origin",
                    "corrupt-origin",
                    "duplicate",
                    "foreign",
                    "unsupported",
                    "missing-successor",
                ):
                    with self.subTest(case=case):
                        fixture.extra_comments = copy.deepcopy(preserved)
                        if case == "missing-origin":
                            del fixture.extra_comments[origin_id]
                        elif case == "corrupt-origin":
                            fixture.extra_comments[origin_id]["body"] += "corrupt"
                        elif case in {"duplicate", "foreign", "unsupported"}:
                            item = copy.deepcopy(native)
                            item["id"] = 9_000_001
                            if case == "foreign":
                                item["user"]["id"] = 9_000_099
                            if case == "unsupported":
                                item["body"] = item["body"].replace(
                                    "reviewsensei:session:v2", "reviewsensei:session:v3"
                                )
                            fixture.extra_comments[item["id"]] = item
                        else:
                            del fixture.extra_comments[native["id"]]
                        fixture.quarantine_predecessor_comment_id = None
                        fixture.select_command("@reviewsensei verify", new_run=True)
                        before = copy.deepcopy(fixture.extra_comments)
                        self.assertEqual(
                            fixture.invoke_command(),
                            1,
                            fixture.error or str(fixture.exception),
                        )
                        self.assertEqual(fixture.extra_comments, before)
                        self.assertFalse(
                            any(
                                method != "GET" and path.startswith("/repos/")
                                for _, method, path in fixture.trace
                            )
                        )
                        self.assertFalse(
                            any(
                                review.get("state") == "APPROVED"
                                for review in fixture.reviews
                            )
                        )
                        self.assertIsNone(
                            old.api_run_accounting["terminal_transaction"]
                        )

    def test_fresh_finding_collision_does_not_discharge_the_historical_obligation(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = JourneyFixture(directory)
            old, registration = fixture.original_origins()
            fixture.provider_comments = [
                {
                    "path": "src/app.py",
                    "line": 2,
                    "side": "RIGHT",
                    "body": "Original published human assessment 0 needs external source validation.",
                    "severity": "high",
                    "blocking": False,
                    "needs_human": True,
                }
            ]
            with registered_resource([registration]):
                self.assertEqual(fixture.invoke(), 0, fixture.error)
                view = approval_eligibility_from_body(
                    fixture.reviews[-1]["body"]
                ).quarantine_assessments
                self.assertTrue(view["fresh_human_pending"])
                self.assertEqual(len(view["pending"]), 2)
                for pending in view["pending"]:
                    fixture.quarantine_predecessor_comment_id = None
                    fixture.select_command(
                        "@reviewsensei dismiss "
                        + pending["instance"]
                        + ' --reason "quarantine:'
                        + pending["origin_scope_sha256"]
                        + ' This exact historical concern has independently verified source evidence."',
                        new_run=True,
                    )
                    self.assertEqual(
                        fixture.invoke_command(),
                        0,
                        fixture.error or str(fixture.exception),
                    )
                self.assertFalse(
                    any(review.get("state") == "APPROVED" for review in fixture.reviews)
                )
                fixture.select_command("@reviewsensei verify", new_run=True)
                self.assertEqual(
                    fixture.invoke_command(), 0, fixture.error or str(fixture.exception)
                )
                current_view = next(
                    approval_eligibility_from_body(
                        review["body"]
                    ).quarantine_assessments
                    for review in reversed(fixture.reviews)
                    if approval_eligibility_from_body(review["body"]) is not None
                )
                self.assertEqual(len(current_view["pending"]), 1)
                self.assertTrue(current_view["fresh_human_pending"])
                self.assertIsNone(old.api_run_accounting["terminal_transaction"])


class QuarantineFullAnalysisRecoveryTests(unittest.TestCase):
    def test_failed_first_full_pass_recovers_exact_context_without_another_provider_call(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            fixture = JourneyFixture(directory)
            old, registration = fixture.original_origins()
            fixture.allow_publication = False
            with registered_resource([registration]):
                self.assertEqual(fixture.invoke(), 1)
                failed = fixture.current_record()
                self.assertEqual(failed.transaction.phase, "publication_failed")
                calls = fixture.provider_calls
                transaction_id = failed.transaction.transaction_id
                fixture.quarantine_predecessor_comment_id = None
                fixture.attempt = 2
                fixture.allow_publication = True
                self.assertEqual(fixture.invoke(), 0, fixture.error)
                self.assertEqual(fixture.provider_calls, calls)
                current = fixture.current_record()
                self.assertEqual(current.transaction.transaction_id, transaction_id)
                self.assertEqual(current.transaction.phase, "publication_succeeded")
                view = approval_eligibility_from_body(
                    fixture.reviews[-1]["body"]
                ).quarantine_assessments
                self.assertEqual(len(view["pending"]), 2)
                self.assertFalse(
                    any(review.get("state") == "APPROVED" for review in fixture.reviews)
                )
                self.assertIsNone(old.api_run_accounting["terminal_transaction"])
                # Repeating the completed same head in a genuinely new run
                # cannot double-wrap or perform another inference pass.
                fixture.past_jobs[fixture.run_id] = "hosted"
                fixture.next_run()
                fixture.attempt = 1
                fixture.requested_head = fixture.live_head = registration["head_sha"]
                self.assertEqual(fixture.invoke(), 0, fixture.error)
                self.assertEqual(fixture.provider_calls, calls)
                # A changed head uses the new complete baseline and an ordinary
                # reservation; the first-full-pass binding is not a permanent
                # bypass of normal convergence.
                previous = fixture.live_head
                fixture.past_jobs[fixture.run_id] = "hosted"
                fixture.next_run()
                git(fixture.root, "checkout", "--detach", previous)
                source = fixture.root / "src/app.py"
                source.write_bytes(source.read_bytes() + b"further\n")
                git(fixture.root, "add", "--", "src/app.py")
                git(fixture.root, "commit", "-m", "next ordinary verification")
                head = git(fixture.root, "rev-parse", "HEAD")
                diff = subprocess.check_output(
                    [
                        "git",
                        "-C",
                        str(fixture.root),
                        "diff",
                        fixture.live_base + "..." + head,
                    ]
                )
                git(fixture.root, "checkout", "--detach", fixture.policy)
                fixture.requested_head = fixture.live_head = head
                (fixture.root / "review.patch").write_bytes(diff)
                self.assertEqual(fixture.invoke(), 0, fixture.error)
                self.assertEqual(fixture.provider_calls, calls + 1)
                from review_sensei.quarantine_lineage import (
                    is_full_analysis_transaction,
                )
                from review_sensei.session import read_session_baseline

                record = fixture.current_record()
                baseline = read_session_baseline(fixture.selected.ledger, record)
                self.assertFalse(
                    is_full_analysis_transaction(record, baseline.cache_key)
                )


class QuarantineFullPassFaultTests(unittest.TestCase):
    def test_partial_first_full_pass_retains_coverage_and_both_historical_holds(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = JourneyFixture(directory)
            old, registration = fixture.original_origins()
            fixture.revision(fixture.live_head, binary=True)
            registration["head_sha"] = fixture.live_head
            fixture.event_document = event(registration)
            with registered_resource([registration]):
                self.assertEqual(fixture.invoke(), 0, fixture.error)
                eligibility = approval_eligibility_from_body(
                    fixture.reviews[-1]["body"]
                )
                self.assertEqual(eligibility.facts.review_status, "partial")
                self.assertEqual(len(eligibility.quarantine_assessments["pending"]), 2)
                self.assertFalse(
                    any(review.get("state") == "APPROVED" for review in fixture.reviews)
                )
                self.assertIsNone(old.api_run_accounting["terminal_transaction"])

    def test_changed_original_admission_context_or_policy_cannot_recover_a_failed_full_pass(
        self,
    ):
        for case in ("key", "configuration"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                fixture = JourneyFixture(directory)
                old, registration = fixture.original_origins()
                fixture.allow_publication = False
                with registered_resource([registration]):
                    self.assertEqual(fixture.invoke(), 1)
                    calls, reviews = fixture.provider_calls, len(fixture.reviews)
                    fixture.quarantine_predecessor_comment_id = None
                    fixture.allow_publication = True
                    fixture.attempt = 2
                    if case == "key":
                        (fixture.root / "AGENTS.md").write_text(
                            "Changed original mandatory admission context.\n",
                            encoding="utf-8",
                            newline="\n",
                        )
                    else:
                        config = fixture.root / ".reviewsensei.yml"
                        config.write_bytes(
                            config.read_bytes() + b"# changed original configuration\n"
                        )
                    self.assertEqual(fixture.invoke(), 1)
                    self.assertEqual(fixture.provider_calls, calls)
                    self.assertEqual(len(fixture.reviews), reviews)
                    self.assertIsNone(old.api_run_accounting["terminal_transaction"])
