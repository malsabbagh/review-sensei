"""Installed decision replay must use GitHub's review-update HTTP contract."""

import copy
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

_TESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TESTS))
from packaging_guard import assert_distribution_import  # noqa: E402

assert_distribution_import()

from review_sensei_github.approval import approval_eligibility_from_result  # noqa: E402
from review_sensei_github.errors import GitHubHTTPTransientError  # noqa: E402
from review_sensei_github.http import GitHubHttp  # noqa: E402
from review_sensei_github.publication import (  # noqa: E402
    IndexedFinding,
    ReviewApprovalFinalizer,
    approval_eligibility_from_body,
    approval_eligibility_marker,
    finding_index_marker,
)

from review_sensei.coverage import CoverageManifest, FileCoverage  # noqa: E402
from review_sensei.disposition import (  # noqa: E402
    FindingIdentity,
    apply_session_command,
    parse_maintainer_command,
    session_dispositions,
)
from review_sensei.models import ReviewComment, ReviewResult  # noqa: E402
from review_sensei.presentation import assign_finding_identifiers  # noqa: E402
from review_sensei.session import InMemorySessionLedger, SessionIdentity  # noqa: E402

HEAD, BASE = "b" * 40, "a" * 40
APP = "reviewsensei[bot]"
PREFIX = "/repos/synthetic/repo"


class Response:
    def __init__(self, value, status=200):
        self.status = status
        self.body = json.dumps(value).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size):
        return self.body[:size]


class ReviewUpdateHost:
    """Strict synthetic GitHub: review summaries accept PUT, never PATCH."""

    def __init__(self, *, status="partial", put_fault=None, changed_identity=None):
        comments = tuple(
            ReviewComment(
                "src/app.py", i + 1, f"Synthetic decision {i}", needs_human=True
            )
            for i in range(15)
        )
        self.eligibility = approval_eligibility_from_result(
            ReviewResult(
                "Synthetic decision review",
                comments,
                "fixture",
                review_status=status,
                coverage=CoverageManifest(
                    files=(FileCoverage("src/app.py", "reviewed"),),
                    enumerated_paths=("src/app.py",),
                ),
            ),
            head_sha=HEAD,
            base_sha=BASE,
            enabled=True,
            app_authored=False,
        )
        findings = self.eligibility.human_review.findings
        self.entries = tuple(
            IndexedFinding(f.fingerprint, f.fingerprint, False, True, body_placed=True)
            for f in findings
        )
        self.review = {
            "id": 5,
            "commit_id": HEAD,
            "state": "COMMENTED",
            "user": {"login": APP},
            "body": approval_eligibility_marker(self.eligibility)
            + "\n"
            + finding_index_marker(
                repository_id=1,
                pull_request=1,
                head_sha=HEAD,
                base_sha=BASE,
                result_digest=self.eligibility.result_digest,
                entries=self.entries,
            ),
        }
        self.ledger = InMemorySessionLedger()
        identity = SessionIdentity("synthetic/repo", 1, repository_id=1)
        inventory = tuple(
            FindingIdentity(e.instance, e.lifecycle, needs_human=True)
            for e in self.entries
        )
        self.ids = assign_finding_identifiers(tuple(e.instance for e in self.entries))
        # Exercise two legal commands, retaining the first batch on replay.
        for batch in (self.entries[:7], self.entries[7:]):
            command = parse_maintainer_command(
                "@sensei override "
                + " ".join(self.ids[e.instance] for e in batch)
                + " acceptable risk",
                actor="alice",
                head_sha=HEAD,
            )
            self.record, _ = apply_session_command(
                self.ledger, identity, command, finding_inventory=inventory
            )
        self.identity = identity
        self.dispositions = session_dispositions(self.record)
        self.put_fault = put_fault
        self.changed_identity = changed_identity
        self.calls, self.approvals, self.checks = [], [], []
        self.http = GitHubHttp(api_url="https://api.github.test", opener=self.open)

    def open(self, request, timeout):
        method, path = request.method, urlparse(request.full_url).path
        self.calls.append((method, path))
        data = json.loads(request.data) if request.data else None
        if method == "GET" and path == PREFIX + "/pulls/1/reviews":
            return Response([self.review, *self.approvals])
        if method == "GET" and path == PREFIX + "/pulls/1":
            repo = {"full_name": "synthetic/repo", "fork": False, "id": 1}
            pull = {
                "state": "open",
                "draft": False,
                "user": {"login": "alice"},
                "head": {"sha": HEAD, "repo": repo},
                "base": {"sha": BASE, "repo": repo},
            }
            if self.changed_identity:
                pull[self.changed_identity]["sha"] = "c" * 40
            return Response(pull)
        if path == PREFIX + "/pulls/1/reviews/5":
            if method == "PATCH":
                return Response({}, 405)
            if method == "PUT":
                if isinstance(self.put_fault, int):
                    return Response({}, self.put_fault)
                if self.put_fault != "conflicting-readback":
                    self.review["body"] = data["body"]
                if self.put_fault == "lost-response":
                    raise GitHubHTTPTransientError("synthetic response loss")
                return Response(self.review)
        if method == "GET" and path == PREFIX + f"/commits/{HEAD}/check-runs":
            return Response({"check_runs": []})
        if method == "POST" and path == PREFIX + "/check-runs":
            self.checks.append(data)
            return Response({"id": 30}, 201)
        if method == "POST" and path == "/graphql":
            assert "mutation" not in data["query"]
            return Response(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "nodes": [],
                                    "pageInfo": {
                                        "hasNextPage": False,
                                        "endCursor": None,
                                    },
                                }
                            }
                        }
                    }
                }
            )
        if method == "POST" and path == PREFIX + "/pulls/1/reviews":
            assert data["event"] == "APPROVE" and data["commit_id"] == HEAD
            review = {"id": 9, "state": "APPROVED", "user": {"login": APP}, **data}
            self.approvals.append(review)
            return Response(review)
        raise AssertionError(f"Unexpected synthetic HTTP operation: {method} {path}")

    def run(self, *, dispositions=None, finding_ids=None):
        return ReviewApprovalFinalizer(http=self.http).refinalize_with_dispositions(
            token="synthetic-review",
            check_token="synthetic-check",
            repository="synthetic/repo",
            repository_id=1,
            pull_request=1,
            head_sha=HEAD,
            app_slug=APP,
            actor="alice",
            finding_ids=tuple(self.ids.values())
            if finding_ids is None
            else finding_ids,
            dispositions=self.dispositions if dispositions is None else dispositions,
        )

    def persisted(self):
        return approval_eligibility_from_body(self.review["body"])


class InstalledReviewUpdateContracts(unittest.TestCase):
    def setUp(self):
        import review_sensei_github

        self.assertTrue(
            Path(review_sensei_github.__file__)
            .resolve()
            .is_relative_to(Path(sys.prefix).resolve())
        )

    def test_fifteen_stored_decisions_use_put_and_partial_stays_withheld(self):
        host = ReviewUpdateHost()
        self.assertEqual(host.ledger.load(host.identity).record, host.record)
        self.assertEqual(len(host.record.dispositions), 15)
        self.assertTrue(all(d.head_sha == HEAD for d in host.dispositions))
        outcome = host.run()
        self.assertEqual(outcome.status, "approval_withheld", outcome.diagnostic)
        self.assertEqual(outcome.diagnostic, "review_incomplete")
        eligibility = host.persisted()
        self.assertEqual(len(eligibility.human_review.resolved), 15)
        self.assertFalse(eligibility.human_review.pending)
        self.assertEqual(eligibility.facts.review_status, "partial")
        self.assertFalse(eligibility.facts.has_human_adjudication_findings)
        self.assertIn(("PUT", PREFIX + "/pulls/1/reviews/5"), host.calls)
        self.assertNotIn(("PATCH", PREFIX + "/pulls/1/reviews/5"), host.calls)
        self.assertEqual(host.checks[-1]["conclusion"], "action_required")
        self.assertEqual(host.checks[-1]["head_sha"], HEAD)
        self.assertFalse(host.approvals)

    def test_complete_eligible_review_approves_once_and_replay_is_idempotent(self):
        host = ReviewUpdateHost(status="complete")
        self.assertEqual(host.run().status, "approved")
        self.assertEqual(host.run().status, "already_approved")
        self.assertEqual(len(host.approvals), 1)
        self.assertEqual(host.checks[-1]["conclusion"], "success")
        self.assertEqual(host.calls.count(("PUT", PREFIX + "/pulls/1/reviews/5")), 1)

    def test_stale_live_head_or_base_prevents_review_update(self):
        for changed in ("head", "base"):
            with self.subTest(changed=changed):
                host = ReviewUpdateHost(changed_identity=changed)
                before = copy.deepcopy(host.review)
                self.assertEqual(host.run().status, "skipped_stale_" + changed)
                self.assertEqual(host.review, before)
                self.assertFalse(any(method == "PUT" for method, _ in host.calls))
                self.assertFalse(host.approvals)
                self.assertEqual(len(host.record.dispositions), 15)

    def test_old_head_decisions_cannot_clear_current_findings(self):
        host = ReviewUpdateHost(status="complete")
        stale = tuple(replace(d, head_sha="c" * 40) for d in host.dispositions)
        self.assertEqual(
            host.run(dispositions=stale, finding_ids=()).status, "approval_withheld"
        )
        self.assertEqual(len(host.persisted().human_review.pending), 15)
        self.assertFalse(host.approvals)
        self.assertEqual(host.checks[-1]["conclusion"], "action_required")

    def test_failed_or_conflicting_put_keeps_decisions_and_withholds_approval(self):
        for fault in (403, 422, 500, "conflicting-readback"):
            with self.subTest(fault=fault):
                host = ReviewUpdateHost(status="complete", put_fault=fault)
                self.assertEqual(host.run().status, "gate_reevaluation_failed")
                self.assertEqual(len(host.persisted().human_review.pending), 15)
                self.assertEqual(len(host.record.dispositions), 15)
                self.assertFalse(host.approvals)
                self.assertFalse(host.checks)
                self.assertEqual(
                    host.calls.count(("PUT", PREFIX + "/pulls/1/reviews/5")), 1
                )
                self.assertEqual(host.ledger.load(host.identity).record, host.record)

    def test_lost_put_response_requires_readback_on_explicit_replay(self):
        host = ReviewUpdateHost(status="complete", put_fault="lost-response")
        self.assertEqual(host.run().status, "gate_reevaluation_failed")
        self.assertFalse(host.persisted().human_review.pending)
        self.assertEqual(len(host.record.dispositions), 15)
        self.assertFalse(host.approvals)
        host.put_fault = None
        self.assertEqual(host.run().status, "approved")
        self.assertEqual(host.calls.count(("PUT", PREFIX + "/pulls/1/reviews/5")), 1)
        self.assertEqual(len(host.approvals), 1)


if __name__ == "__main__":
    unittest.main()
