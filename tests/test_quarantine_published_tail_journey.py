"""Real CLI journey for a published unfinished tail and four ceased replies."""

import base64
import copy
import hashlib
import itertools
import json
import random
import re
import string
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch
from urllib.parse import urlsplit

import test_quarantine_journey as journey_contract
from fake_github_http import json_response
from review_sensei_github.api_run_requests import ApiRunRequestTransport
from review_sensei_github.publication import (
    approval_eligibility_from_body,
    approval_eligibility_marker,
    parse_finding_index,
)
from review_sensei_github.quarantine_published_tail import (
    BROKER_QUALIFICATIONS,
    CALLER_BLOB,
    execution_manifest_digest,
)
from review_sensei_github.session_ledger import parse_session_comment
from test_quarantine_activation import event, registered_resource
from test_quarantine_evidence import original_objects
from test_quarantine_journey import JourneyFixture
from test_quarantine_published_tail_isolation import contract_fixture

from review_sensei.bounded_evidence import canonical_bytes

_RUNS = itertools.count(3900)


class PublishedTailJourneyFixture(JourneyFixture):
    tail_runs = None

    def creator(self):
        return {**super().creator(), "created_at": "2026-10-28T00:01:00Z"}

    def select_command(self, *args, **kwargs):
        super().select_command(*args, **kwargs)
        if getattr(self, "late_effects", None) is not None:
            return
        self.late_effects = []
        self.resolved_threads.update(self.old_thread_ids)
        self.late_effects.append("old-explicit-thread-resolves")
        for original in self.old_reviews:
            eligibility = approval_eligibility_from_body(original["body"])
            if eligibility is None or eligibility.human_review is None:
                continue
            resolved = replace(
                eligibility.human_review,
                resolved=tuple(
                    f.fingerprint for f in eligibility.human_review.findings
                ),
            )
            settled = replace(
                eligibility,
                human_review=resolved,
                facts=replace(eligibility.facts, has_human_adjudication_findings=False),
            )
            late = copy.deepcopy(original)
            late["id"] = 8_000_000 + len(self.reviews)
            late["body"] = re.sub(
                r"<!-- reviewsensei:eligibility:v1 [A-Za-z0-9_=-]+ -->",
                approval_eligibility_marker(settled),
                late["body"],
            )
            self.reviews.append(late)
        self.late_effects.append("old-head-appended-resolution-receipts")
        self.extra_comments[8_100_000] = {
            "id": 8_100_000,
            "body": "Late old bot reply: @reviewsensei verify. This is not a new human decision.",
            "user": {"id": 999, "login": "reviewsensei[bot]", "type": "Bot"},
            "issue_url": "https://api.github.test/repos/owner/repo/issues/7",
        }
        self.late_effects.extend(("old-reply-comment", "old-reaction-and-metadata"))

    def external(self, request, timeout):
        path = urlsplit(request.full_url).path
        if self.tail_runs is not None and request.get_method() == "GET":
            if path.endswith("/check-runs") and "/commits/" in path:
                assert (
                    request.get_header("Authorization")
                    != "Bearer synthetic-workflow-read"
                )
                self._meter(request)
                head = path.split("/commits/", 1)[1].split("/", 1)[0]
                rows = [
                    copy.deepcopy(item)
                    for item in self.check_runs
                    if item.get("head_sha") == head
                ]
                return json_response({"total_count": len(rows), "check_runs": rows})
            if path.endswith("/reactions"):
                self._meter(request)
                return json_response([])
            if path == "/repos/owner/repo/actions/runs":
                self._meter(request)
                return json_response(
                    {"workflow_runs": list(self.tail_runs.values()), "total_count": 5}
                )
            for run_id, run in self.tail_runs.items():
                if path == f"/repos/owner/repo/actions/runs/{run_id}":
                    self._meter(request)
                    return json_response(copy.deepcopy(run))
                if path == f"/repos/owner/repo/actions/runs/{run_id}/attempts/1/jobs":
                    self._meter(request)
                    return json_response(copy.deepcopy(self.tail_jobs[run_id]))
            if path.endswith("/contents/.github/workflows/review-sensei-review.yml"):
                self._meter(request)
                return json_response(
                    {
                        "type": "file",
                        "path": ".github/workflows/review-sensei-review.yml",
                        "sha": CALLER_BLOB,
                    }
                )
        return super().external(request, timeout)

    def original_origins(self, *, failed_count=9, published_count=5):
        original_objects(self)
        first_head = self.live_head
        self.provider_comments = [
            {
                "path": "src/app.py",
                "line": 2,
                "side": "RIGHT",
                "body": f"Earlier published assessment {n} requires independent human judgment.",
                "severity": "high",
                "blocking": False,
                "needs_human": True,
            }
            for n in range(published_count)
        ]
        assert self.invoke(human=True) == 0, self.error
        self.next_run()
        second_head = self.revision(first_head)
        self.provider_comments = [
            {
                "path": "src/app.py",
                "line": 3,
                "side": "RIGHT",
                "body": f"Current published assessment {n} requires independent human judgment.",
                "severity": "high",
                "blocking": False,
                "needs_human": True,
            }
            for n in range(failed_count)
        ]
        with patch.object(
            ApiRunRequestTransport, "finish_review_run", side_effect=KeyboardInterrupt
        ):
            assert self.invoke(human=True) == 130, self.error
        old = parse_session_comment(
            self.comment["body"], identity=self.selected.identity
        )
        assert old.transaction.phase == "publication_succeeded"
        assert old.api_run_accounting["terminal_transaction"] is None
        self.source_previous = old
        self.old_reviews = copy.deepcopy(self.reviews)
        self.old_thread_ids = {"PRRT_" + str(i["id"]) for i in self.inline}
        self.next_run()
        self.run_id = self.creation_run = next(_RUNS)
        # A genuinely different, immutable current head; no old thread is reused.
        from test_quarantine_evidence import git

        git(self.root, "checkout", "--detach", second_head)
        (self.root / "src/app.py").write_text("keep\nchange\nmore\nfresh\nnext\n")
        git(self.root, "add", "src/app.py")
        git(self.root, "commit", "-m", "new current head after ceased published tail")
        current = git(self.root, "rev-parse", "HEAD")
        self.revision(current)
        self.event_name = "workflow_dispatch"
        self.quarantine_predecessor_comment_id = 1_000_000
        _, scope, runs, inventories, qualification = contract_fixture()
        manifest = scope["published_tail"]
        self.tail_runs, self.tail_jobs = {}, {}
        for i, item in enumerate(manifest["executions"]):
            run_id = old.api_run_accounting["identity"]["run_id"] + i
            item["run_id"] = run_id
            item["configuration"] = old.api_run_accounting["configuration"]
            item["accounting_snapshot"] = old.api_run_accounting if i == 0 else None
            runs[i].update({"id": run_id, "head_sha": old.transaction.head_sha})
            for job in inventories[i]["jobs"]:
                job["run_id"] = run_id
            self.tail_runs[run_id], self.tail_jobs[run_id] = runs[i], inventories[i]
        qualification["execution_manifest_sha256"] = execution_manifest_digest(manifest)
        manifest["broker_qualification_sha256"] = hashlib.sha256(
            canonical_bytes(qualification)
        ).hexdigest()
        BROKER_QUALIFICATIONS[manifest["broker_qualification_sha256"]] = qualification
        registration = {
            "schema_version": "quarantine-published-activation-v1",
            "published_tail": manifest,
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
        return old, registration


class NativePublishedTailJourneyFixture(PublishedTailJourneyFixture):
    """Synthetic V2 source qualification plus actual native selected fields.

    This explicitly declares encoded reply bounds at the external qualification
    boundary; it is not a production provenance packet or an API-side receipt.
    The complete caller, ledger, staging, publisher and readers remain real.
    """

    def original_origins(self, **kwargs):
        old, registration = super().original_origins(**kwargs)
        manifest = registration["published_tail"]
        qualification = copy.deepcopy(
            BROKER_QUALIFICATIONS[manifest["broker_qualification_sha256"]]
        )
        roots = [row for row in self.inline if row.get("in_reply_to_id") is None]
        for execution in manifest["executions"]:
            if execution["source"] is not None:
                execution["source"]["root_id"] = roots[-1]["id"]
        digest = execution_manifest_digest(manifest)
        surfaces = qualification["exposure"]["surfaces"]
        replies = surfaces["inline_replies"]["new_objects"]
        surfaces["reviews"]["new_objects"] += replies
        surfaces["reviews"]["new_wire_bytes"] += replies * 1024
        qualification["schema_version"] = "quarantine-published-tail-provenance-v2"
        qualification["execution_manifest_sha256"] = digest
        qualification["reader_projection"] = {
            "schema_version": "native-reader-projection-v1",
            "repository_id": 123,
            "pull_request": 7,
            "producer_id": 999,
            "execution_manifest_sha256": digest,
            "source_contract_sha256": qualification["exposure"][
                "source_contract_sha256"
            ],
            "roots": [
                {
                    "root_id": row["id"],
                    "thread_id": "PRRT_" + str(row["id"]),
                    "review_id": row["pull_request_review_id"],
                    "head_sha": row["commit_id"],
                }
                for row in roots
            ],
            "executions": [
                {
                    "run_id": execution["run_id"],
                    "route": execution["route"],
                    "writer_source_sha256": qualification["exposure"][
                        "source_contract_sha256"
                    ],
                    "explicit_review_head_sha": old.transaction.head_sha,
                    "check_head_sha": old.transaction.head_sha,
                    "reply_parent_root_ids": [row["id"] for row in roots],
                    "resolution_root_ids": [row["id"] for row in roots],
                    "new_thread_roots": 0,
                }
                for execution in manifest["executions"]
            ],
            "reply_review_allocation": {
                "explicit_new_objects": 8,
                "explicit_new_wire_bytes": 8 * 65536,
                "implicit_new_objects": replies,
                "implicit_new_wire_bytes": replies * 1024,
                "implicit_max_object_wire_bytes": 1024,
                "implicit_head_binding": "api-selected-head",
            },
        }
        manifest["broker_qualification_sha256"] = hashlib.sha256(
            canonical_bytes(qualification)
        ).hexdigest()
        BROKER_QUALIFICATIONS[manifest["broker_qualification_sha256"]] = qualification
        self.event_document = event(registration)
        return old, registration

    def external(self, request, timeout):
        from types import SimpleNamespace

        from review_sensei_github.http import GitHubHttp
        from review_sensei_github.native_review_census import (
            CLOSE_MIDDLE_QUERY,
            CLOSE_QUERY,
            source_query_variables,
        )
        from review_sensei_github.native_review_groups import PART_QUERY
        from test_native_review_census import NativeCensusBackend

        path = urlsplit(request.full_url).path
        if request.get_method() == "POST" and path == "/graphql":
            submitted = json.loads(request.data)
            query, variables = submitted.get("query"), submitted.get("variables")
            if query == PART_QUERY or source_query_variables(query, variables):
                from review_sensei_github.native_review_census import (
                    root_ids_base_query,
                )

                query = root_ids_base_query(query)
                self._meter(request)
                assert self.accounted.observations[-1]["mutation"] is False
                snapshot = object.__new__(NativeCensusBackend)
                snapshot.http = GitHubHttp(api_url="https://api.github.test")
                snapshot.plan = SimpleNamespace(
                    repository_id=123,
                    pull_request=7,
                    head_sha=self.live_head,
                    base_sha=self.live_base,
                )
                snapshot.calls, snapshot.reviews, snapshot.group_payloads = [], [], []
                snapshot.old_rows, snapshot.old_nodes, snapshot.roots = [], {}, []

                def selected(comment):
                    parent = next(
                        row
                        for row in self.reviews
                        if row["id"] == comment["pull_request_review_id"]
                    )
                    reply = comment.get("in_reply_to_id")
                    return {
                        "n": str(comment["id"]),
                        "b": comment["body"],
                        "p": comment["path"],
                        "l": comment.get("line"),
                        "o": comment.get("original_line", comment.get("line")),
                        "a": {"t": comment["user"]["type"], "n": comment["user"]["id"]},
                        "c": {"o": parent["commit_id"]},
                        "z": {"o": parent["commit_id"]},
                        "r": {"n": str(parent["id"])},
                        "q": None if reply is None else {"n": str(reply)},
                        "u": comment.get("updated_at", "2026-10-28T00:00:00Z"),
                    }

                for ordinal, review in enumerate(self.reviews):
                    comments = [
                        selected(row)
                        for row in self.inline
                        if row["pull_request_review_id"] == review["id"]
                    ]
                    author = {"t": review["user"]["type"], "n": review["user"]["id"]}
                    metadata = {
                        "i": "part:" + str(ordinal),
                        "n": str(review["id"]),
                        "s": review["state"],
                        "a": author,
                        "c": {"o": review["commit_id"]},
                        "x": {
                            "totalCount": len(comments),
                            "nodes": [
                                {"n": row["n"], "q": row["q"]} for row in comments[:1]
                            ],
                        },
                    }
                    snapshot.reviews.append(metadata)
                    snapshot.group_payloads.append(
                        {
                            "data": {
                                "node": {
                                    "n": metadata["n"],
                                    "b": review["body"],
                                    "s": review["state"],
                                    "a": author,
                                    "c": metadata["c"],
                                    "p": {"n": 7, "r": {"n": 123}},
                                    "x": {
                                        "totalCount": len(comments),
                                        "nodes": comments[:100],
                                        "pageInfo": {
                                            "hasNextPage": len(comments) > 100,
                                            "endCursor": "end",
                                        },
                                    },
                                }
                            }
                        }
                    )

                def actual_root(row):
                    actual = next(
                        item for item in self.inline if item["id"] == int(row["n"])
                    )
                    return {
                        **NativeCensusBackend.root(row),
                        "s": "PRRT_" + row["n"] in self.resolved_threads,
                        "d": actual.get("side", "RIGHT"),
                    }

                # Both the root metadata and the old-body nodes select the
                # same live thread resolution; the generic backend defaults
                # to unresolved and must not invent a stale false value here.
                snapshot.root = actual_root
                wanted = set(variables.get("roots", []))
                for comment in self.inline:
                    if comment.get("in_reply_to_id") is not None:
                        continue
                    row = selected(comment)
                    root = snapshot.root(row)
                    thread = "PRRT_" + str(comment["id"])
                    root["s"] = thread in self.resolved_threads
                    root["d"] = comment.get("side", "RIGHT")
                    snapshot.roots.append(root)
                    if thread in wanted:
                        snapshot.old_rows.append(row)
                        snapshot.old_nodes[comment["id"]] = thread
                snapshot.main_id, snapshot.main_body = 1, ""
                snapshot.thread_ids = {
                    int(row["x"]["nodes"][0]["n"]): "PRRT_"
                    + str(row["x"]["nodes"][0]["n"])
                    for row in snapshot.roots
                }
                snapshot.change = None
                if query in {CLOSE_QUERY, CLOSE_MIDDLE_QUERY}:
                    frames = [
                        self.reviews[int(node.split(":")[1])]
                        for node in variables["frames"]
                    ]
                    snapshot.main_id, snapshot.main_body = (
                        frames[0]["id"],
                        frames[0]["body"],
                    )

                    def close_frames(_query, response):
                        response["data"]["frames"] = [
                            {
                                "n": str(row["id"]),
                                "b": row["body"],
                                "s": row["state"],
                                "a": {"t": row["user"]["type"], "n": row["user"]["id"]},
                                "c": {"o": row["commit_id"]},
                                "p": {"n": 7, "r": {"n": 123}},
                                "x": {
                                    "totalCount": sum(
                                        item["pull_request_review_id"] == row["id"]
                                        for item in self.inline
                                    )
                                },
                            }
                            for row in frames
                        ]

                    snapshot.change = close_frames
                return snapshot.opened(request, timeout)
        before = len(self.inline)
        response = super().external(request, timeout)
        if request.get_method() == "POST" and path.endswith("/pulls/7/reviews"):
            payload = json.loads(request.data)
            for comment in self.inline[before:]:
                comment["commit_id"] = payload["commit_id"]
            if payload["event"] == "REQUEST_CHANGES":
                self.reviews[-1]["state"] = "CHANGES_REQUESTED"
        return response


class PublishedTailJourneyTests(unittest.TestCase):
    exercise_counts = journey_contract.QuarantineJourneyTests.exercise_counts

    def test_late_archive_and_operation_allocations_fit_the_unchanged_complete_journey(
        self,
    ):
        with patch.dict(BROKER_QUALIFICATIONS, {}, clear=True):
            self.exercise_counts(
                9, 5, delayed_old=True, fixture_type=HeavyPublishedTailJourneyFixture
            )

    # Reuse the unchanged public-entry journey oracle without inheriting the
    # unrelated literal-failure tests or duplicating their expensive runs.
    def test_empty_fresh_review_cannot_clear_both_origins_and_only_exact_decisions_allow_approval(
        self,
    ):
        with patch.dict(BROKER_QUALIFICATIONS, {}, clear=True):
            self.exercise_counts(
                9, 5, delayed_old=True, fixture_type=PublishedTailJourneyFixture
            )

    def test_stored_and_unstored_creation_acknowledgments_never_create_twice(self):
        for loss in ("stored", "absent"):
            with (
                self.subTest(loss=loss),
                tempfile.TemporaryDirectory() as directory,
                patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
            ):
                fixture = PublishedTailJourneyFixture(directory)
                old, registration = fixture.original_origins()
                old_body, provider_calls = (
                    fixture.comment["body"],
                    fixture.provider_calls,
                )
                fixture.creation_fault = loss
                with registered_resource([registration]):
                    result = fixture.invoke()
                    self.assertEqual(
                        result, 0 if loss == "stored" else 1, fixture.error
                    )
                    self.assertEqual(fixture.root_posts, 1)
                    self.assertEqual(fixture.comment["body"], old_body)
                    self.assertIsNone(old.api_run_accounting["terminal_transaction"])
                    self.assertEqual(
                        fixture.provider_calls, provider_calls + (loss == "stored")
                    )
                    self.assertEqual(fixture.invoke(), 1)
                    self.assertEqual(fixture.root_posts, 1)

    def test_fresh_recurrence_stays_independent_of_fourteen_historical_decisions(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            fixture = NativePublishedTailJourneyFixture(directory)
            old, registration = fixture.original_origins()
            fixture.provider_comments = [
                {
                    "path": "src/app.py",
                    "line": 3,
                    "side": "RIGHT",
                    "body": "Current published assessment 0 requires independent human judgment.",
                    "severity": "high",
                    "blocking": False,
                    "needs_human": True,
                }
            ]
            with registered_resource([registration]):
                self.assertEqual(fixture.invoke(), 0, fixture.error)
                # Inspect the displayed current obligations structurally. The
                # unaware parser must still refuse v8; runtime commands below
                # must use the independently authenticated complete union.
                from review_sensei_github.approval import ReviewApprovalEligibility
                from review_sensei_github.native_union import (
                    native_union_frame_document,
                )

                self.assertIsNone(
                    approval_eligibility_from_body(fixture.reviews[-1]["body"])
                )
                observed = native_union_frame_document(fixture.reviews[-1]["body"])
                view = ReviewApprovalEligibility.from_dict(
                    observed["current"]
                ).quarantine_assessments
                self.assertEqual(len(view["pending"]), 14)
                self.assertTrue(view["fresh_human_pending"])
                index = parse_finding_index(
                    fixture.reviews[-1]["body"],
                    repository_id=123,
                    pull_request=7,
                    head_sha=fixture.live_head,
                )
                fresh = index.entries[0]
                authenticated = fixture.selected.ledger._quarantine_authenticated
                historical = [
                    a
                    for inventory in authenticated.inventories.values()
                    for a in inventory["assessments"]
                ]
                self.assertTrue(
                    any(a["instance"] == fresh.instance for a in historical)
                )
                self.assertTrue(
                    any(
                        a["instance"] != fresh.instance
                        and a["concern"] == fresh.lifecycle
                        for a in historical
                    )
                )
                original_dispositions = fixture.current_record().dispositions
                original_threads = {"PRRT_" + str(i["id"]) for i in fixture.inline[:-1]}
                fresh_thread = "PRRT_" + str(fixture.inline[-1]["id"])
                self.assertNotIn(fresh_thread, original_threads)
                for number, pending in enumerate(view["pending"]):
                    fixture.quarantine_predecessor_comment_id = None
                    fixture.select_command(
                        (
                            "@reviewsensei dismiss "
                            if number % 2 == 0
                            else "@reviewsensei accept-risk "
                        )
                        + pending["instance"]
                        + ' --reason "quarantine:'
                        + pending["origin_scope_sha256"]
                        + ' Original source evidence disproves this historical concern only."',
                        new_run=True,
                    )
                    self.assertEqual(
                        fixture.invoke_command(),
                        0,
                        fixture.error or str(fixture.exception),
                    )
                current_review = max(
                    (r for r in fixture.reviews if r["commit_id"] == fixture.live_head),
                    key=lambda r: r["id"],
                )
                observed = native_union_frame_document(current_review["body"])
                current = ReviewApprovalEligibility.from_dict(observed["current"])
                self.assertFalse(current.quarantine_assessments["pending"])
                self.assertTrue(current.quarantine_assessments["fresh_human_pending"])
                self.assertFalse(current.human_review.resolved)
                self.assertEqual(
                    fixture.current_record().dispositions, original_dispositions
                )
                settlements = copy.deepcopy(
                    fixture.selected.ledger._quarantine_authenticated.state[
                        "settlements"
                    ]
                )
                self.assertEqual(len(settlements), 14)
                self.assertEqual(
                    {d["action"] for d in settlements}, {"dismiss", "accept-risk"}
                )
                self.assertNotIn(fresh_thread, fixture.resolved_threads)
                self.assertFalse(
                    any(
                        r["state"] == "APPROVED" and r["commit_id"] == fixture.live_head
                        for r in fixture.reviews
                    )
                )
                self.assertEqual(
                    [
                        c
                        for c in fixture.check_runs
                        if c["head_sha"] == fixture.live_head
                    ][-1]["conclusion"],
                    "action_required",
                )
                fixture.select_command(fixture.command_body, new_run=True)
                self.assertEqual(
                    fixture.invoke_command(), 0, fixture.error or str(fixture.exception)
                )
                self.assertEqual(
                    fixture.selected.ledger._quarantine_authenticated.state[
                        "settlements"
                    ],
                    settlements,
                )
                fixture.select_command("@reviewsensei verify", new_run=True)
                self.assertEqual(
                    fixture.invoke_command(), 0, fixture.error or str(fixture.exception)
                )
                self.assertFalse(
                    any(
                        r["state"] == "APPROVED" and r["commit_id"] == fixture.live_head
                        for r in fixture.reviews
                    )
                )
                self.assertIsNone(old.api_run_accounting["terminal_transaction"])
                fixture.select_command(
                    "@reviewsensei dismiss "
                    + fresh.instance
                    + ' --reason "The current source and exact diff establish this current recurrence is intentional."',
                    new_run=True,
                )
                self.assertEqual(
                    fixture.invoke_command(), 0, fixture.error or str(fixture.exception)
                )
                self.assertEqual(
                    sum(
                        r["state"] == "APPROVED" and r["commit_id"] == fixture.live_head
                        for r in fixture.reviews
                    ),
                    1,
                )


class HeavyPublishedTailJourneyFixture(PublishedTailJourneyFixture):
    def select_command(self, *args, **kwargs):
        super().select_command(*args, **kwargs)
        if getattr(self, "late_allocations_materialized", False):
            return
        self.late_allocations_materialized = True
        from review_sensei_github.operation_host import (
            RESULT_COMMENT_MARKER,
            render_marked_comment,
        )
        from review_sensei_github.quarantine_creation import _body
        from review_sensei_github.quarantine_origin import source_binding

        from review_sensei.bounded_evidence import partition_evidence

        entropy = random.Random(37)
        binding = source_binding(self.source_previous, "github-bot:999")
        bodies = []
        for number in range(9):
            document = {
                "late_old_source": number,
                "opaque": "".join(
                    entropy.choices(string.ascii_letters + string.digits, k=30000)
                ),
            }
            parts = partition_evidence(document, binding=binding, item_count=1)
            assert len(parts) == 1
            bodies.append(_body(parts[0]))
        for _ in range(4):
            data = entropy.randbytes(44000)
            bodies.append(
                render_marked_comment(
                    RESULT_COMMENT_MARKER,
                    {
                        "kind": "result",
                        "event_key": "d" * 64 + "\t" + "e" * 64,
                        "result_sha256": hashlib.sha256(data).hexdigest(),
                        "index": 0,
                        "count": 1,
                        "data": base64.b64encode(data).decode(),
                    },
                )
            )
        for number, body in enumerate(bodies):
            identifier = 8_200_000 + number
            self.extra_comments[identifier] = {
                "id": identifier,
                "body": body,
                "user": {"id": 999, "login": "reviewsensei[bot]", "type": "Bot"},
                "issue_url": "https://api.github.test/repos/owner/repo/issues/7",
                "created_at": "2026-10-28T00:02:00Z",
                "updated_at": "2026-10-28T00:02:00Z",
            }
