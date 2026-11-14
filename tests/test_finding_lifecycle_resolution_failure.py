"""Cold current-route resolution faults preserve hidden durable obligations."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse

from tests import test_finding_lifecycle_reply_restart as reply_fixture


def fault_worker(root: Path, fault: str) -> None:
    original = reply_fixture.ReplyService._open

    def faulty(self, request, timeout=None):
        body = json.loads(request.data) if request.data else {}
        operation = body.get("operationName") if isinstance(body, dict) else None
        if operation == "ResolveReviewThread":
            from review_sensei_github.errors import GitHubHTTPTransientError

            if fault == "lost-stored":
                original(self, request, timeout)
                self.save()
            else:
                self.calls.append(
                    (request.method, urlparse(request.full_url).path, body)
                )
            raise GitHubHTTPTransientError("synthetic resolution response lost")
        return original(self, request, timeout)

    reply_fixture.ReplyService._open = faulty
    reply_fixture.worker(root, "inline", "reply")


class CurrentResolutionFailureTests(unittest.TestCase):
    def test_lost_resolution_keeps_hidden_obligation_and_native_truth(self):
        for fault in ("lost-stored", "lost-unstored"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                first = reply_fixture.FindingReplyRestartTests().phase(
                    root, "inline", "produce"
                )
                self.assertIsNone(first["error"])
                process = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "tests.test_finding_lifecycle_resolution_failure",
                        str(root),
                        fault,
                    ],
                    cwd=Path(__file__).resolve().parents[1],
                    env=os.environ.copy(),
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(process.returncode, 0, process.stderr)
                after = json.loads(process.stdout)
                self.assertFalse(
                    any(r["state"] == "APPROVED" for r in after["reviews"])
                )
                hidden = next(
                    o for o in first["state"]["obligations"] if o["symbol"] == "conn"
                )
                retained = next(
                    o for o in after["state"]["obligations"] if o["id"] == hidden["id"]
                )
                self.assertEqual(retained["criterion"], hidden["criterion"])
                self.assertNotEqual(retained["status"], "verified-fixed")
                mutations = [
                    call
                    for call in after["calls"]
                    if isinstance(call[2], dict)
                    and call[2].get("operationName") == "ResolveReviewThread"
                ]
                self.assertEqual(len(mutations), 1)
                self.assertEqual(
                    after["threads"][0]["isResolved"], fault == "lost-stored"
                )
                if fault == "lost-unstored":
                    self.assertIsNone(after["outcome"])
                    self.assertIn("native intent remains uncertain", after["error"])
                    self.assertTrue(
                        any(
                            effect["status"] == "pending"
                            and effect["request"].get("operationName")
                            == "ResolveReviewThread"
                            for effect in after["durable"]["effects"]
                        )
                    )
                else:
                    self.assertIsNone(after["error"])
                    self.assertEqual(after["outcome"]["resolution_status"], "resolved")
                self.assertLessEqual(len(after["calls"]), 64)


if __name__ == "__main__":
    fault_worker(Path(sys.argv[1]), sys.argv[2])
