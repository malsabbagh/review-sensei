"""Partial, no-wildcard c7 failure through the real review/quarantine entries."""

import hashlib
import itertools
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from fake_github_http import json_response
from review_sensei_github.publication import (
    ReviewPublisher,
    approval_eligibility_from_body,
)
from review_sensei_github.quarantine_published_tail import (
    BODY_CALLER_BLOB,
    BODY_CONTRACT,
    BROKER_QUALIFICATIONS,
    execution_manifest_digest,
)
from review_sensei_github.session_ledger import parse_session_comment
from test_quarantine_activation import event, registered_resource
from test_quarantine_evidence import original_objects
from test_quarantine_isolation import run as qualified_run
from test_quarantine_journey import JourneyFixture
from test_quarantine_published_tail_isolation import contract_fixture

from review_sensei.bounded_evidence import canonical_bytes

_RUNS = itertools.count(7900)


class BodyLimitJourneyFixture(JourneyFixture):
    def creator(self):
        return {
            **super().creator(),
            "run_number": 7,
            "created_at": "2026-10-28T00:01:00Z",
        }

    def external(self, request, timeout):
        old = self.source_previous
        path = urlsplit(request.full_url).path
        if old is not None and request.get_method() == "GET":
            if path == "/repos/owner/repo/actions/runs":
                self._meter(request)
                return json_response(
                    {"workflow_runs": [self.old_execution()], "total_count": 1}
                )
            if path.endswith("/check-runs") and "/commits/" in path:
                assert (
                    request.get_header("Authorization")
                    != "Bearer synthetic-workflow-read"
                )
                self._meter(request)
                head = path.split("/commits/", 1)[1].split("/", 1)[0]
                rows = [
                    item for item in self.check_runs if item.get("head_sha") == head
                ]
                return json_response({"total_count": len(rows), "check_runs": rows})
        response = super().external(request, timeout)
        if (
            old is not None
            and request.get_method() == "GET"
            and path
            == f"/repos/owner/repo/actions/runs/{old.api_run_accounting['identity']['run_id']}"
        ):
            return json_response(self.old_execution())
        return response

    def old_execution(self):
        value = qualified_run()
        value.update(
            {
                "id": self.source_previous.api_run_accounting["identity"]["run_id"],
                "head_sha": self.source_previous.transaction.head_sha,
                "created_at": "2026-10-28T00:00:00Z",
            }
        )
        value["referenced_workflows"][0]["sha"] = (
            "c7f13b8cfe28e89ec82ad03bd2f34aed5b40cbb1"
        )
        return value

    def original_body_failure(self):
        original_objects(self)
        self.revision(self.live_head, binary=True)
        self.provider_summary = "Original non-authoritative narrative. " * 350
        self.provider_comments = [
            {
                "path": "src/app.py",
                "side": "FILE",
                "body": f"Original candidate {i}: " + "bounded finding evidence " * 20,
                "severity": "high" if i < 3 else "low",
                "blocking": False,
                "needs_human": i < 3,
            }
            for i in range(80)
        ]
        original = ReviewPublisher.publish

        def old_formatter(publisher, **kwargs):
            kwargs["narrative_archive"] = None
            return original(publisher, **kwargs)

        with patch.object(ReviewPublisher, "publish", old_formatter):
            assert self.invoke() == 1, self.error
        assert "framed review body" in self.error, self.error
        old = parse_session_comment(
            self.comment["body"], identity=self.selected.identity
        )
        assert old.transaction.phase == "publication_failed"
        self.source_previous = old
        self.next_run()
        self.run_id = self.creation_run = next(_RUNS)
        self.revision(old.transaction.head_sha, remove_binary=True)
        self.event_name = "workflow_dispatch"
        self.quarantine_predecessor_comment_id = 1_000_000
        registration = {
            "schema_version": "quarantine-body-limit-activation-v2",
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
            "run_number": 7,
        }
        _, scope, _, _, qualification = contract_fixture()
        manifest = scope["published_tail"]
        manifest.update(
            {"schema_version": BODY_CONTRACT, "caller_blob": BODY_CALLER_BLOB}
        )
        primary = manifest["executions"][0]
        primary.update(
            {
                "run_id": old.api_run_accounting["identity"]["run_id"],
                "route": "failed-review",
                "conclusion": "failure",
                "configuration": old.api_run_accounting["configuration"],
                "accounting_snapshot": old.api_run_accounting,
            }
        )
        manifest["executions"] = [primary]
        qualification.update(
            {
                "contract": BODY_CONTRACT,
                "execution_manifest_sha256": execution_manifest_digest(manifest),
            }
        )
        digest = hashlib.sha256(canonical_bytes(qualification)).hexdigest()
        manifest["broker_qualification_sha256"] = digest
        BROKER_QUALIFICATIONS[digest] = qualification
        registration["prepublication"] = manifest
        self.creation_metadata = self.creator()
        self.event_document = event(registration)
        self.provider_comments, self.provider_summary = [], None
        return old, registration


class BodyLimitQuarantineTests(unittest.TestCase):
    fixture_type = BodyLimitJourneyFixture

    def test_partial_eighty_candidate_origin_survives_empty_full_successor_and_decisions(
        self,
    ):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            f = self.fixture_type(directory)
            if hasattr(f, "bridge"):
                self.addCleanup(f.bridge.close)
            old, registration = f.original_body_failure()
            old_body = f.comment["body"]
            calls = f.provider_calls
            with registered_resource([registration]):
                self.assertEqual(f.invoke(), 0, f.error)
                authenticated = f.selected.ledger._quarantine_authenticated
                source = next(iter(authenticated.inventories))
                archive = authenticated.archive
                self.assertEqual(
                    archive["target_isolation"]["contract"],
                    "body-limit-prepublication-v1",
                )
                self.assertEqual(
                    archive["target_isolation"]["body_limit_failure"][
                        "raw_candidate_count"
                    ],
                    80,
                )
                self.assertEqual(
                    archive["target_isolation"]["body_limit_failure"][
                        "retained_review_status"
                    ],
                    "partial",
                )
                origin = archive["assessment_origins"]["origins"][0]
                retained = f.selected.ledger.read_evidence(
                    f.selected.identity,
                    origin["source_manifest"],
                    expected_binding=origin["source_manifest"]["binding"],
                )
                self.assertEqual(len(retained["retained_result"]["comments"]), 80)
                self.assertEqual(
                    retained["retained_result"]["review_status"], "partial"
                )
                self.assertFalse(
                    any(
                        any(c in item["path"] for c in "*?[")
                        for item in retained["retained_result"]["comments"]
                    )
                )
                pending = approval_eligibility_from_body(
                    f.reviews[-1]["body"]
                ).quarantine_assessments["pending"]
                self.assertEqual(len(pending), 3)
                self.assertEqual(f.provider_calls, calls + 1)
                self.assertFalse(any(r["state"] == "APPROVED" for r in f.reviews))
                for item in pending:
                    f.quarantine_predecessor_comment_id = None
                    f.select_command(
                        "@reviewsensei dismiss "
                        + item["instance"]
                        + ' --reason "quarantine:'
                        + source
                        + ' Original source disproves only this historical concern."',
                        new_run=True,
                    )
                    self.assertEqual(f.invoke_command(), 0, f.error or str(f.exception))
                self.assertEqual(
                    len(
                        f.selected.ledger._quarantine_authenticated.state["settlements"]
                    ),
                    3,
                )
                self.assertEqual(sum(r["state"] == "APPROVED" for r in f.reviews), 1)
                f.select_command("@reviewsensei verify", new_run=True)
                self.assertEqual(f.invoke_command(), 0, f.error or str(f.exception))
                f.select_command(f.command_body, new_run=True)
                f.command_active, f.owner_job = False, "hosted"
                f.event_name, f.event_document = "pull_request", None
                self.assertEqual(f.invoke(), 0, f.error)
                self.assertEqual(sum(r["state"] == "APPROVED" for r in f.reviews), 1)
                self.assertEqual(f.comment["body"], old_body)
                self.assertIsNone(old.api_run_accounting["terminal_transaction"])
