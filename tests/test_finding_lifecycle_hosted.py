"""Ordinary native-ledger lifecycle across independent worker interpreters.

Only fake remote-service/result JSON bytes survive workers. The native marker
adapter is production code. This does not replace run-owner/broker deployment
qualification or claim that a raw token fixture authenticates an Actions run.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

from review_sensei_github import (
    ConversationPublisher,
    GitHubApplication,
    GitHubWriteOptions,
    LearningPRPublisher,
    ReviewPublisher,
)
from review_sensei_github.session_ledger import GitHubIssueCommentSessionLedger

from review_sensei.baseline import baseline_from_review
from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.context import ReviewContextCacheKey
from review_sensei.models import ReviewResult, ReviewTransaction
from review_sensei.session import (
    checkpoint_review_analysis,
    prepare_review_transaction,
    session_reservation_id,
)
from tests import test_active_window_lifecycle_g10 as window_fixtures
from tests.test_active_window_lifecycle_g10 import (
    BASE_SHA,
    ORDINARY_DIFF,
    _comment,
    _coverage,
    _result,
    _sha,
    _WindowFakeBroker,
)
from tests.test_durable_finding_lifecycle_restart import (
    IDENTITY,
    PersistentNativeService,
    _state_observation,
)
from tests.test_review_transaction import CONFIGURATION, EVIDENCE, POLICY


class HostedNativeService(PersistentNativeService):
    def _open(self, request, timeout=None):
        path = urlparse(request.full_url).path
        response = super()._open(request, timeout)
        # The remote service authors issue association/timestamps on each actual
        # created or changed issue comment. Lifecycle code never invents them.
        if request.method in {"POST", "PATCH"} and "/issues/" in path:
            for comment in self.issue_comments:
                comment.setdefault(
                    "issue_url", "https://api.github.test/repos/owner/repo/issues/1"
                )
                comment.setdefault("created_at", "2026-11-12T12:00:00Z")
                comment["updated_at"] = "2026-11-12T12:00:00Z"
            self.save()
            if request.method == "POST":
                return self._resp(self.issue_comments[-1], 201)
            cid = int(path.rsplit("/", 1)[-1])
            return self._resp(next(c for c in self.issue_comments if c["id"] == cid))
        return response


def hosted_worker(root: Path, action: str):
    head = ("2" if action == "omitted" else "1") * 40
    if action == "replay":
        head = json.loads((root / "result.json").read_text())["transaction"]["head_sha"]
    host = HostedNativeService(root / "service.json", head)
    budget = EvidenceReadBudget()
    ledger = GitHubIssueCommentSessionLedger(
        host.http,
        token="fixture-token",
        app_slug="reviewsensei[bot]",
        evidence_budget=budget,
        enable_finding_lifecycle_writes=True,
    )
    publisher = ReviewPublisher(http=host.http)
    app = GitHubApplication(
        http=host.http,
        broker=_WindowFakeBroker(),
        reviewer=publisher,
        learner=LearningPRPublisher(http=host.http),
        replier=ConversationPublisher(http=host.http),
        session_ledger=ledger,
    )
    options = GitHubWriteOptions(
        auto_review=True,
        auto_approve=True,
        github_writes=True,
        operation_entry="disabled",
        max_active_findings=1,
    )
    before_calls = len(host.calls)
    error = None
    stage = "enrollment"
    outcome = None
    before = None
    try:
        if action == "initial":
            ledger.initialize(IDENTITY)
        else:
            loaded = ledger.load(IDENTITY)
            before = loaded.record.to_dict() if loaded.record else None
        stage = "admission"
        key = ReviewContextCacheKey(
            repository="owner/repo",
            pull_request=1,
            base_sha=BASE_SHA,
            head_sha=head,
            engine="fixture",
            model="fixture-model",
            profile="default",
            stage_digest=_sha("stage"),
            context_digest=_sha("context"),
            learning_digest=_sha("learning"),
        )
        if action == "replay":
            result = ReviewResult.from_dict(
                json.loads((root / "result.json").read_text())
            )
        else:
            comments = (
                (
                    _comment(
                        "src/app.py",
                        line=5,
                        symbol="auth",
                        body="Required auth check.",
                        severity="critical",
                    ),
                    _comment(
                        "src/db.py",
                        line=5,
                        symbol="conn",
                        body="Required cleanup.",
                        severity="high",
                    ),
                )
                if action == "initial"
                else ()
            )
            result = replace(
                _result(*comments, coverage=_coverage("src/app.py", "src/db.py")),
                model="fixture-model",
            )
            prepared = prepare_review_transaction(
                ledger,
                IDENTITY,
                POLICY,
                reservation_id=session_reservation_id(
                    repository="owner/repo",
                    pull_request=1,
                    head_sha=head,
                    kind="publish",
                ),
                base_sha=BASE_SHA,
                head_sha=head,
                configuration_digest=ReviewTransaction.compute_configuration_digest(
                    CONFIGURATION
                ),
                evidence_digest=ReviewTransaction.compute_evidence_digest(EVIDENCE),
            )
            stage = "analysis-checkpoint"
            result = checkpoint_review_analysis(
                ledger,
                IDENTITY,
                prepared,
                result,
                baseline=baseline_from_review(
                    result,
                    cache_key=key,
                    policy=POLICY,
                    generation=prepared.record.generation + 1,
                ),
            )
            (root / "result.json").write_text(json.dumps(result.to_dict()))
        stage = "ordinary-publication"
        outcome = app.publish_review(
            publication_budget=budget,
            options=options,
            oidc_token="fixture-oidc",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha=head,
            base_branch="main",
            base_sha=BASE_SHA,
            result=result,
            diff=ORDINARY_DIFF,
            app_slug="reviewsensei[bot]",
            convergence_policy=POLICY,
            current_key=key,
            configuration_context=CONFIGURATION,
            evidence_context=EVIDENCE,
            blocker_candidates=window_fixtures.OrdinaryPublisherActiveWindowTests._blockers(
                result
            ),
        )
        stage = "complete"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    host.save()
    # Observation uses actual persisted service bytes, never reloads mutable
    # state into the publisher and never mints another write allowance.
    from review_sensei_github.session_ledger import parse_session_comment

    records = []
    for comment in host.issue_comments:
        if "<!-- reviewsensei:session:" in comment["body"]:
            try:
                records.append(
                    parse_session_comment(comment["body"], identity=IDENTITY).to_dict()
                )
            except Exception:
                pass
    print(
        json.dumps(
            {
                "error": error,
                "stage": stage,
                "status": getattr(outcome, "status", None),
                "diagnostic": getattr(outcome, "diagnostic", None),
                "calls": host.calls[before_calls:],
                "evidence_calls": budget.calls,
                "record": records[0] if len(records) == 1 else None,
                "before_record": before,
                "state": _state_observation(publisher, "active"),
                "reviews": host.reviews,
                "threads": host.threads,
            }
        )
    )


class HostedFindingLifecycleTests(unittest.TestCase):
    def phase(self, root, action):
        repo = Path(__file__).resolve().parents[1]
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            (
                str(repo),
                str(repo / "src"),
                str(repo / "packages/python/review-sensei-github/src"),
                str(repo / "tests"),
            )
        )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "tests.test_finding_lifecycle_hosted",
                str(root),
                action,
            ],
            cwd=repo,
            env=env,
            text=True,
            capture_output=True,
            timeout=40,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_native_marker_initial_cold_omission_and_exact_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self.phase(root, "initial")
            self.assertIsNone(first["error"], first)
            self.assertEqual(first["status"], "published")
            self.assertEqual(first["evidence_calls"], 54)
            self.assertEqual(len(first["reviews"]), 1)
            self.assertEqual(len(first["threads"]), 1)
            self.assertEqual(first["record"]["completed_initial_reviews"], 1)
            self.assertEqual(len(first["state"]["obligations"]), 2)
            second = self.phase(root, "omitted")
            self.assertIsNone(second["error"], second)
            self.assertEqual(second["status"], "published")
            self.assertEqual(second["evidence_calls"], 56)
            self.assertEqual(len(second["threads"]), 1)
            self.assertEqual(second["record"]["completed_initial_reviews"], 1)
            self.assertEqual(second["record"]["completed_verification_rounds"], 1)
            self.assertEqual(
                {(o["id"], o["criterion"]) for o in first["state"]["obligations"]},
                {(o["id"], o["criterion"]) for o in second["state"]["obligations"]},
            )
            self.assertTrue(
                all(
                    o["status"] == "omitted-uncertain"
                    for o in second["state"]["obligations"]
                )
            )
            self.assertEqual(first["threads"][0]["id"], second["threads"][0]["id"])
            replay = self.phase(root, "replay")
            self.assertIsNone(replay["error"], replay)
            self.assertEqual(replay["status"], "already_published")
            self.assertEqual(second["record"], replay["record"])
            self.assertEqual(second["reviews"], replay["reviews"])
            self.assertEqual(second["threads"], replay["threads"])
            self.assertTrue(
                all(method == "GET" for method, path, body in replay["calls"])
            )

    def test_mutated_marker_cannot_resume_the_retained_obligations(self):
        for mode in ("edited", "duplicate", "foreign-producer"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                first = self.phase(root, "initial")
                self.assertEqual(len(first["reviews"]), 1)
                service_path = root / "service.json"
                service = json.loads(service_path.read_text())
                marker = next(
                    c
                    for c in service["issue_comments"]
                    if "<!-- reviewsensei:session:" in c["body"]
                )
                if mode == "edited":
                    marker["body"] = marker["body"].replace(
                        '"completed_initial_reviews":1', '"completed_initial_reviews":0'
                    )
                elif mode == "duplicate":
                    service["issue_comments"].append(
                        {**marker, "id": marker["id"] + 100}
                    )
                else:
                    marker["user"] = {**marker["user"], "id": 9002}
                service_path.write_text(json.dumps(service))
                second = self.phase(root, "replay")
                self.assertTrue(
                    second["error"] or second["status"] == "handoff", second
                )
                self.assertEqual(second["reviews"], first["reviews"])
                self.assertEqual(second["threads"], first["threads"])
                self.assertFalse(
                    any(
                        method in {"POST", "PATCH", "PUT", "DELETE"}
                        for method, path, body in second["calls"]
                    ),
                    second,
                )

    def test_inline_proof_pins_every_read_and_refuses_frontier_growth(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self.phase(root, "initial")
            self.assertIsNone(first["error"])
            host = HostedNativeService(root / "service.json", "1" * 40)
            ledger = GitHubIssueCommentSessionLedger(
                host.http,
                token="fixture-token",
                app_slug="reviewsensei[bot]",
                evidence_budget=EvidenceReadBudget(),
                enable_finding_lifecycle_writes=True,
            )
            record = ledger.load(IDENTITY).record
            plan = ledger.preflight_finding_lifecycle_inline(record)
            self.assertEqual(plan["conditional_calls"], 4)
            before = len(host.calls)
            changed = ledger.replace_finding_lifecycle_inline(
                IDENTITY,
                lambda selected: selected.evolve(generation=selected.generation + 1),
                record=record,
            )
            calls = host.calls[before:]
            self.assertEqual([m for m, p, b in calls], ["GET", "GET", "PATCH", "GET"])
            self.assertEqual(ledger.evidence_budget.calls, 5)
            # A service-side concurrent growth appears on the first sealed
            # discovery; one request refuses, without fallback or root mutation.
            for number in range(99):
                host.issue_comments.append(
                    {
                        "id": 10000 + number,
                        "body": "human comment",
                        "user": {"id": 100, "login": "human", "type": "User"},
                    }
                )
            before = len(host.calls)
            from review_sensei.errors import ReviewInputError

            with self.assertRaisesRegex(
                ReviewInputError, "single-page frontier changed"
            ):
                ledger.replace_finding_lifecycle_inline(
                    IDENTITY,
                    lambda selected: selected.evolve(
                        generation=selected.generation + 1
                    ),
                    record=changed,
                )
            self.assertEqual([m for m, p, b in host.calls[before:]], ["GET"])
            self.assertIsNone(getattr(ledger, "_finding_lifecycle_page_size", None))


if __name__ == "__main__":
    hosted_worker(Path(sys.argv[1]), sys.argv[2])
