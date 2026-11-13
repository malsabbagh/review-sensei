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
        path = urlparse(request.full_url).path
        if request.method == "GET" and "/pulls/comments/" in path:
            self.calls.append(("GET", path, None))
            cid = int(path.rsplit("/", 1)[-1])
            comment = next(
                (
                    c
                    for t in self.threads
                    for c in t["comments"]["nodes"]
                    if c["databaseId"] == cid
                ),
                None,
            )
            self.save()
            return self._resp(
                dict(comment, id=cid) if comment else {"message": "Not Found"},
                200 if comment else 404,
            )
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
    error = None
    outcome = None
    try:
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
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    host.save()
    print(
        json.dumps(
            {
                "status": outcome.status if outcome else None,
                "diagnostic": outcome.diagnostic if outcome else None,
                "error": error,
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

    def test_cold_drift_readback_uses_exact_target_without_retry(self):
        for mutation in ("edited", "foreign", "duplicated", "deleted"):
            with (
                self.subTest(mutation=mutation),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                fixture.DurableFindingRestartTests.run_phase(
                    self, root, "drift", "initial"
                )
                self.run_worker(root, "lost", expected_exit=73)
                path = root / "service.json"
                service = json.loads(path.read_text())
                comment = service["threads"][0]["comments"]["nodes"][0]
                if mutation == "edited":
                    comment["body"] += "\nHuman changed this evidence"
                elif mutation == "foreign":
                    comment["author"]["databaseId"] = 31337
                    comment["user"]["id"] = 31337
                elif mutation == "duplicated":
                    duplicate = json.loads(json.dumps(service["threads"][0]))
                    duplicate["id"] = "PRRT_duplicate_native"
                    duplicate["comments"]["nodes"][0]["databaseId"] += 1
                    service["threads"].append(duplicate)
                else:
                    service["threads"].clear()
                path.write_text(json.dumps(service))
                replay = self.run_worker(root, "replay")
                if mutation == "duplicated":
                    # A second object does not invalidate the exact known native ID.
                    # It also cannot substitute for or authorize edits to that target.
                    self.assertIsNone(replay["error"], replay)
                else:
                    self.assertIn("native intent remains uncertain", replay["error"])
                self.assertEqual(replay["threads"], service["threads"])
                self.assertEqual(replay["reviews"], service["reviews"])
                self.assertFalse(
                    any(
                        method != "GET"
                        and (
                            path != "/graphql" or "mutation" in payload.get("query", "")
                        )
                        for method, path, payload in replay["calls"]
                    )
                )
                self.assertFalse(
                    any(r["state"] == "APPROVED" for r in replay["reviews"])
                )
                self.assertTrue(
                    any(
                        e["status"]
                        == ("confirmed" if mutation == "duplicated" else "pending")
                        and e["method"] == "PATCH"
                        and "/pulls/comments/" in e["path"]
                        for e in replay["durable"]["effects"]
                    )
                )

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
