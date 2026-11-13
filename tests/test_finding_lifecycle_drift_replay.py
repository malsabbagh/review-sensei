"""Cold exact reconciliation of an owned drift edit with a lost response."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse

from tests import test_durable_finding_lifecycle_restart as fixture


def _worker(root: Path, action: str):
    native_open = fixture.PersistentNativeService._open

    def retain_check_identity(self, request, timeout=None):
        response = native_open(self, request, timeout)
        if request.method == "POST" and urlparse(request.full_url).path.endswith(
            "/check-runs"
        ):
            payload = json.loads(request.data)
            self.check_runs[-1]["external_id"] = payload["external_id"]
            self.save()
        return response

    fixture.PersistentNativeService._open = retain_check_identity
    if action == "lost":
        original = fixture.PersistentNativeService._open

        def lose_after_patch(self, request, timeout=None):
            response = original(self, request, timeout)
            if (
                request.method == "PATCH"
                and "/pulls/comments/" in urlparse(request.full_url).path
            ):
                self.save()
                os._exit(73)
            return response

        fixture.PersistentNativeService._open = lose_after_patch
        fixture._worker(root, "drift", "rename", {})
        return
    from review_sensei_github import (
        ConversationPublisher,
        GitHubApplication,
        GitHubWriteOptions,
        LearningPRPublisher,
        ReviewPublisher,
    )

    from review_sensei.finding_drift import FileDriftDescriptor
    from review_sensei.models import ReviewResult
    from review_sensei.session import LocalSessionLedger

    head = "2" * 40
    host = fixture.PersistentNativeService(root / "service.json", head)
    ledger = LocalSessionLedger(root / "ledger", enable_finding_lifecycle_writes=True)
    publisher = ReviewPublisher(http=host.http)
    app = GitHubApplication(
        http=host.http,
        broker=fixture._WindowFakeBroker(),
        reviewer=publisher,
        learner=LearningPRPublisher(http=host.http),
        replier=ConversationPublisher(http=host.http),
        session_ledger=ledger,
    )
    result = ReviewResult.from_dict(json.loads((root / "result.json").read_text()))
    before = len(host.calls)
    outcome = app.publish_review(
        options=GitHubWriteOptions(
            auto_review=True,
            auto_approve=True,
            github_writes=True,
            operation_entry="disabled",
            max_active_findings=1,
            enable_drift_tracking=True,
        ),
        oidc_token="fixture-oidc",
        repository="owner/repo",
        repository_id=1,
        pull_request=1,
        head_sha=head,
        base_branch="main",
        base_sha=fixture.BASE_SHA,
        result=result,
        diff=fixture.ORDINARY_DIFF.replace("src/app.py", "src/renamed.py"),
        drift_descriptors=(
            FileDriftDescriptor(
                "file_rename", "src/app.py", ("src/renamed.py",), ("auth",)
            ),
        ),
        app_slug="reviewsensei[bot]",
        convergence_policy=fixture.POLICY,
        configuration_context=fixture.CONFIGURATION,
        evidence_context=fixture.EVIDENCE,
        blocker_candidates=fixture.window_fixtures.OrdinaryPublisherActiveWindowTests._blockers(
            result
        ),
    )
    host.save()
    print(
        json.dumps(
            {
                "status": outcome.status,
                "diagnostic": outcome.diagnostic,
                "calls": host.calls[before:],
                "state": fixture._state_observation(publisher, "drift"),
                "durable": fixture._durable_observation(root),
                "threads": host.threads,
                "reviews": host.reviews,
            }
        )
    )


class DriftEditRestartTests(unittest.TestCase):
    def run_worker(self, root, action, expected_exit=0):
        process = subprocess.run(
            [sys.executable, "-m", __name__, str(root), action],
            cwd=Path(__file__).resolve().parents[1],
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=90,
        )
        self.assertEqual(process.returncode, expected_exit, process.stderr)
        return json.loads(process.stdout) if process.stdout.strip() else None

    def test_cold_lost_stored_patch_reconciles_without_second_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initial = fixture.DurableFindingRestartTests.run_phase(
                self, root, "drift", "initial"
            )
            original = initial["state"]["obligations"]
            self.run_worker(root, "lost", expected_exit=73)
            pending = fixture._durable_observation(root)
            patches = [
                e
                for e in pending["effects"]
                if e["method"] == "PATCH" and "/pulls/comments/" in e["path"]
            ]
            self.assertEqual(len(patches), 1)
            self.assertEqual(patches[0]["status"], "pending")
            self.assertIn("$body_sha256", patches[0]["request"]["body"])
            replay = self.run_worker(root, "replay")
            self.assertEqual(replay["status"], "published", replay)
            self.assertFalse(
                any(
                    m == "PATCH" and "/pulls/comments/" in p
                    for m, p, _ in replay["calls"]
                ),
                replay["calls"],
            )
            self.assertEqual(len(replay["threads"]), 1)
            rows = replay["state"]["obligations"]
            self.assertEqual({o["id"] for o in rows}, {o["id"] for o in original})
            self.assertEqual(len(rows), 2)
            self.assertFalse(any(r["state"] == "APPROVED" for r in replay["reviews"]))
            self.assertTrue(
                all(e["status"] != "pending" for e in replay["durable"]["effects"])
            )


if __name__ == "__main__":
    _worker(Path(sys.argv[1]), sys.argv[2])
