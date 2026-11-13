"""Cold ordinary reply and human assessment consumers of durable findings."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict, replace
from functools import wraps
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

from review_sensei_github import (
    ConversationPublisher,
    GitHubApplication,
    GitHubWriteOptions,
    ReviewPublisher,
)

from review_sensei.baseline import baseline_from_review
from review_sensei.context import ReviewContextCacheKey
from review_sensei.models import ProviderResponse, ReviewTransaction
from review_sensei.session import (
    LocalSessionLedger,
    checkpoint_review_analysis,
    prepare_review_transaction,
    session_reservation_id,
)
from tests import test_active_window_lifecycle_g10 as fixtures
from tests import test_durable_finding_lifecycle_restart as lifecycle_fixtures
from tests.test_active_window_lifecycle_g10 import (
    BASE_SHA,
    ORDINARY_DIFF,
    _comment,
    _result,
    _sha,
    _WindowFakeBroker,
)
from tests.test_durable_finding_lifecycle_restart import (
    IDENTITY,
    PersistentNativeService,
    _durable_observation,
    _state_observation,
)
from tests.test_review_transaction import CONFIGURATION, EVIDENCE, POLICY

HEAD = "1" * 40
PATCH = "@@ -1,5 +1,5 @@\n-a\n+reject_remote_requests(data)\n b\n c\n d\n e"
SOURCE = "@reviewsensei This path is intentionally local-only; reject_remote_requests(data) runs before any data leaves the machine."


class ReplyService(PersistentNativeService):
    def __init__(self, path, *, kind, lose=False, crash=False):
        super().__init__(path, HEAD)
        self.kind, self.lose = kind, lose
        self.crash = crash
        self.source = {
            "id": 77,
            "body": SOURCE,
            "created_at": "2026-11-12T12:00:00Z",
            "updated_at": "2026-11-12T12:00:00Z",
            "user": {"id": 55, "login": "alice", "type": "User"},
            "author_association": "MEMBER",
            "issue_url": "https://api.github.test/repos/owner/repo/issues/1",
            "pull_request_url": "https://api.github.test/repos/owner/repo/pulls/1",
        }
        if self.threads:
            self.source["in_reply_to_id"] = self.threads[0]["comments"]["nodes"][0][
                "databaseId"
            ]

    def _roots(self):
        return [
            dict(
                c,
                id=c["databaseId"],
                created_at="2026-11-12T11:00:00Z",
                updated_at="2026-11-12T11:00:00Z",
                commit_id=HEAD,
                original_commit_id=HEAD,
                diff_hunk=PATCH,
            )
            for t in self.threads
            for c in t["comments"]["nodes"]
            if "databaseId" in c
        ]

    def _open(self, request, timeout=None):
        path = urlparse(request.full_url).path
        body = json.loads(request.data) if request.data else None
        if request.method == "GET":
            value = None
            if path.endswith("/comments/77"):
                value = self.source
            elif path.endswith("/pulls/1/comments"):
                value = [
                    *self._roots(),
                    self.source,
                    *[c for c in self.issue_comments if c.get("in_reply_to_id")],
                ]
            elif path.endswith("/issues/1/comments"):
                value = (
                    [self.source, *self.issue_comments]
                    if self.kind == "human"
                    else self.issue_comments
                )
            elif "/pulls/comments/" in path:
                cid = int(path.rsplit("/", 1)[-1])
                value = next(
                    (
                        c
                        for c in [*self._roots(), *self.issue_comments]
                        if c["id"] == cid
                    ),
                    None,
                )
            elif path.endswith("/pulls/1/files"):
                value = [
                    {
                        "filename": p,
                        "status": "modified",
                        "patch": PATCH,
                        "additions": 1,
                        "deletions": 1,
                    }
                    for p in ("src/app.py", "src/db.py")
                ]
            elif "/contents/" in path:
                self.calls.append((request.method, path, body))
                self.save()
                return self._resp({}, 404)
            elif path.endswith("/reactions"):
                value = []
            if value is not None:
                self.calls.append((request.method, path, body))
                self.save()
                return self._resp(value)
        if request.method == "POST" and path.endswith("/reactions"):
            self.calls.append((request.method, path, body))
            self.save()
            return self._resp({"id": 900}, 201)
        if request.method == "DELETE" and "/reactions/" in path:
            self.calls.append((request.method, path, body))
            self.save()
            return self._resp({}, 204)
        if request.method == "POST" and (
            path.endswith("/replies") or path.endswith("/issues/1/comments")
        ):
            self.calls.append((request.method, path, body))
            item = {
                "id": 8800 + len(self.issue_comments),
                "body": body["body"],
                "created_at": "2026-11-12T12:01:00Z",
                "updated_at": "2026-11-12T12:01:00Z",
                "user": {"id": 9001, "login": self.app_slug, "type": "Bot"},
                "issue_url": self.source["issue_url"],
                "pull_request_url": self.source["pull_request_url"],
            }
            if path.endswith("/replies"):
                item["in_reply_to_id"] = self.source["in_reply_to_id"]
            self.issue_comments.append(item)
            self.save()
            if self.crash:
                os._exit(74)
            if self.lose:
                self.lose = False
                from review_sensei_github.errors import GitHubHTTPTransientError

                raise GitHubHTTPTransientError(
                    "reply response lost after persisted effect"
                )
            return self._resp(item, 201)
        return super()._open(request, timeout)


class EvidenceProvider:
    name = "fixture"
    model = "fixture-model"

    def __init__(self, path):
        self.calls = 0
        self.path = path

    def complete(self, request):
        self.calls += 1
        count = int(self.path.read_text()) if self.path.exists() else 0
        self.path.write_text(str(count + 1))
        if '"pending"' in request.prompt:
            value, _ = json.JSONDecoder().raw_decode(
                request.prompt[request.prompt.index('{"head_sha"') :]
            )
            payload = {
                "body": "The current patch verifies the assessment.",
                "assessments": [
                    {
                        "fingerprint": item["fingerprint"],
                        "decision": "addressed",
                        "rationale": "The guard rejects remote requests before transmission.",
                        "human_evidence": "This path is intentionally local-only",
                        "diff_evidence": "reject_remote_requests(data)",
                    }
                    for item in value["pending"]
                ],
            }
        else:
            payload = {
                "body": "The current guard rejects remote requests before transmission.",
                "resolve": True,
            }
        return ProviderResponse(json.dumps(payload), "fixture", "fixture-model")


def worker(root, kind, action):
    if action == "produce":
        host = PersistentNativeService(root / "service.json", HEAD)
    else:
        host = ReplyService(
            root / "service.json",
            kind=kind,
            lose=action == "lose",
            crash=action == "crash_reply",
        )
    ledger = LocalSessionLedger(root / "ledger", enable_finding_lifecycle_writes=True)
    reviewer = ReviewPublisher(http=host.http)
    app = GitHubApplication(
        http=host.http,
        broker=_WindowFakeBroker(),
        reviewer=reviewer,
        learner=None,
        replier=ConversationPublisher(http=host.http),
        session_ledger=ledger,
    )
    options = GitHubWriteOptions(
        github_writes=True,
        auto_review=True,
        auto_approve=True,
        mention_replies=True,
        operation_entry="disabled",
        max_active_findings=1,
    )
    provider = EvidenceProvider(root / "provider-count")
    error, outcome = None, None
    before = len(host.calls)
    try:
        if action in {"produce", "warm"}:
            ledger.initialize(IDENTITY)
            first = _comment(
                "src/app.py",
                line=5,
                symbol="auth",
                body="Confirm remote requests are rejected before transmission.",
                severity="critical",
            )
            if kind == "human":
                first = replace(
                    first, blocking=False, effective_blocking=False, needs_human=True
                )
            result = replace(
                _result(
                    first,
                    _comment(
                        "src/db.py",
                        line=5,
                        symbol="conn",
                        body="Required cleanup.",
                        severity="high",
                    ),
                ),
                model="fixture-model",
            )
            key = ReviewContextCacheKey(
                repository="owner/repo",
                pull_request=1,
                base_sha=BASE_SHA,
                head_sha=HEAD,
                engine="fixture",
                model="fixture-model",
                profile="default",
                stage_digest=_sha("stage"),
                context_digest=_sha("context"),
                learning_digest=_sha("learning"),
            )
            prepared = prepare_review_transaction(
                ledger,
                IDENTITY,
                POLICY,
                reservation_id=session_reservation_id(
                    repository="owner/repo",
                    pull_request=1,
                    head_sha=HEAD,
                    kind="publish",
                ),
                base_sha=BASE_SHA,
                head_sha=HEAD,
                configuration_digest=ReviewTransaction.compute_configuration_digest(
                    CONFIGURATION
                ),
                evidence_digest=ReviewTransaction.compute_evidence_digest(EVIDENCE),
            )
            baseline = baseline_from_review(
                result,
                cache_key=key,
                policy=POLICY,
                generation=prepared.record.generation + 1,
            )
            result = checkpoint_review_analysis(
                ledger, IDENTITY, prepared, result, baseline=baseline
            )
            blockers = fixtures.OrdinaryPublisherActiveWindowTests._blockers(result)
            if kind == "human":
                blockers = (replace(blockers[0], needs_human=True), *blockers[1:])
            produced_publishers = []
            original_publish = ReviewPublisher.publish

            @wraps(original_publish)
            def capture_producer(owner, **kwargs):
                produced_publishers.append(owner)
                return original_publish(owner, **kwargs)

            with patch.object(ReviewPublisher, "publish", capture_producer):
                outcome = app.publish_review(
                    options=options,
                    oidc_token="fixture",
                    repository="owner/repo",
                    repository_id=1,
                    pull_request=1,
                    head_sha=HEAD,
                    base_branch="main",
                    base_sha=BASE_SHA,
                    result=result,
                    diff=ORDINARY_DIFF,
                    app_slug=host.app_slug,
                    convergence_policy=POLICY,
                    current_key=key,
                    configuration_context=CONFIGURATION,
                    evidence_context=EVIDENCE,
                    blocker_candidates=blockers,
                )
            if action == "warm":
                # Keep the application/publisher warm, but open the new reply
                # invocation's ordinary local ledger transport. Reusing the
                # spent publication allowance would correctly remain refused.
                assert produced_publishers[-1]._native_finding_context is None
                ledger = LocalSessionLedger(
                    root / "ledger", enable_finding_lifecycle_writes=True
                )
                app.session_ledger = ledger
                if kind == "inline":
                    host.source["in_reply_to_id"] = host.threads[0]["comments"][
                        "nodes"
                    ][0]["databaseId"]
        if action != "produce":
            outcome = app.generate_and_publish_reply(
                options=options,
                oidc_token="fixture",
                read_token="read",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                source_comment_id=77,
                source_updated_at=host.source["updated_at"],
                expected_head_sha=HEAD,
                reply_provider=provider,
                model="fixture-model",
                app_slug=host.app_slug,
                root_comment_id=(
                    77 if kind == "human" else host.source["in_reply_to_id"]
                ),
                source_kind="issue" if kind == "human" else "inline",
            )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    host.save()
    print(
        json.dumps(
            {
                "error": error,
                "outcome": asdict(outcome) if outcome else None,
                "provider_calls": provider.calls,
                "state": _state_observation(reviewer, "window"),
                "durable": _durable_observation(root),
                "record": (
                    lambda selected: (
                        selected.record.to_dict()
                        if selected.record
                        else {"status": selected.status}
                    )
                )(ledger.load(IDENTITY)),
                "calls": host.calls[before:],
                "reviews": host.reviews,
                "threads": host.threads,
            }
        )
    )


class FindingReplyRestartTests(unittest.TestCase):
    def phase(self, root, kind, action, *, expected_exit=0):
        repo = Path(__file__).resolve().parents[1]
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "tests.test_finding_lifecycle_reply_restart",
                str(root),
                kind,
                action,
            ],
            cwd=repo,
            env={
                **os.environ,
                "PYTHONPATH": os.pathsep.join(
                    str(repo / p)
                    for p in (
                        "src",
                        "packages/python/review-sensei-github/src",
                        ".",
                        "tests",
                    )
                ),
            },
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(process.returncode, expected_exit, process.stderr)
        return json.loads(process.stdout) if expected_exit == 0 else None

    def test_warm_publisher_reply_and_human_resolution(self):
        for kind in ("inline", "human"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                result = self.phase(Path(directory), kind, "warm")
                self.assertIsNone(result["error"])
                self.assertEqual(result["outcome"]["resolution_status"], "resolved")
                self.assertIsNotNone(result["outcome"]["finalization_completion"])
                self.assertTrue(result["threads"][0]["isResolved"])
                self.assertFalse(
                    any(r["state"] == "APPROVED" for r in result["reviews"])
                )

    def test_reply_dispatch_process_death_keeps_obligations_and_deduplicates(self):
        for kind in ("inline", "human"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                first = self.phase(root, kind, "produce")
                self.phase(root, kind, "crash_reply", expected_exit=74)
                service = json.loads((root / "service.json").read_text())
                self.assertFalse(
                    any(r["state"] == "APPROVED" for r in service["reviews"])
                )
                replay = self.phase(root, kind, "reply")
                self.assertIsNone(replay["error"])
                updated = json.loads((root / "service.json").read_text())
                self.assertEqual(
                    len(updated["issue_comments"]), len(service["issue_comments"])
                )
                self.assertEqual(int((root / "provider-count").read_text()), 1)
                hidden_id = next(
                    o["id"]
                    for o in first["state"]["obligations"]
                    if o["symbol"] == "conn"
                )
                self.assertTrue(
                    any(
                        o["id"] == hidden_id and o["status"] != "verified-fixed"
                        for o in replay["state"]["obligations"]
                    )
                )
                self.assertFalse(
                    any(r["state"] == "APPROVED" for r in replay["reviews"])
                )

    def test_cold_reply_and_human_assessment_preserve_hidden_obligation(self):
        for kind in ("inline", "human"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                first = self.phase(root, kind, "produce")
                self.assertIsNone(first["error"])
                self.assertEqual(first["outcome"]["status"], "published")
                before = {o["symbol"]: o for o in first["state"]["obligations"]}
                replied = self.phase(root, kind, "reply")
                self.assertIsNone(replied["error"])
                self.assertIn(
                    replied["outcome"]["status"], {"replied", "already_replied"}
                )
                self.assertEqual(replied["outcome"]["resolution_status"], "resolved")
                self.assertTrue(replied["threads"][0]["isResolved"])
                self.assertIsNotNone(replied["outcome"]["finalization_completion"])

                after = {o["symbol"]: o for o in replied["state"]["obligations"]}
                self.assertEqual(after["auth"]["status"], "verified-fixed")
                self.assertEqual(
                    after["auth"]["criterion"], before["auth"]["criterion"]
                )
                self.assertEqual(after["conn"]["id"], before["conn"]["id"])
                self.assertNotEqual(after["conn"]["status"], "verified-fixed")
                self.assertFalse(
                    any(r["state"] == "APPROVED" for r in replied["reviews"])
                )
                promoted = lifecycle_fixtures.DurableFindingRestartTests().run_phase(
                    root, "window", "omitted"
                )
                self.assertIsNone(promoted["error"])
                self.assertEqual(promoted["status"], "published")
                self.assertEqual(promoted["state"]["active"], [before["conn"]["id"]])
                self.assertEqual(int((root / "provider-count").read_text()), 1)


if __name__ == "__main__":
    worker(Path(sys.argv[1]), sys.argv[2], sys.argv[3])
