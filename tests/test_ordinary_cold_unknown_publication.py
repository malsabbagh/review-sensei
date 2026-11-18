"""Cold ordinary execution cannot erase an absent unknown publication intent."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from review_sensei_github import ordinary_publication_plan as planning
from review_sensei_github.session_ledger import parse_session_comment
from test_api_work_persistence import IDENTITY
from test_review_run import EntryFixture

from tests.test_ordinary_cold_location_drift import CHILD
from tests.test_ordinary_cold_structural_drift import DELETE
from tests.test_ordinary_native_recovery_cold import subprocess_environment


class OrdinaryColdUnknownPublicationTests(unittest.TestCase):
    def test_changed_head_after_absent_unknown_review_keeps_pending_intent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            f = EntryFixture(str(source), limit=512)
            f.active_finding_cap = 1
            f.provider_comments = [
                {
                    "path": "src/app.py",
                    "line": 2,
                    "side": "RIGHT",
                    "body": "Confirm the caller interface contract.",
                    "severity": "high",
                    "blocking": False,
                    "needs_human": True,
                }
            ]
            opened = f.external
            dispatches = []
            journals = []
            original_admit = planning.admit_current

            def absent(request, timeout):
                path = urlsplit(request.full_url).path
                if request.get_method() == "POST" and path.endswith("/pulls/7/reviews"):
                    f.trace.append((f.attempt, "POST", path))
                    dispatches.append(path)
                    raise TimeoutError("synthetic absent unknown review")
                return opened(request, timeout)

            def capture(publisher, observation, journal, **kwargs):
                journals.append(journal)
                return original_admit(publisher, observation, journal, **kwargs)

            f.external = absent
            with patch.object(planning, "admit_current", capture):
                code = f.invoke()
            self.assertEqual(code, 1, f.error)
            self.assertEqual(len(dispatches), 1)
            self.assertEqual(f.reviews, [])
            pending = [e for e in journals[0].effects if e["status"] == "pending"]
            self.assertEqual(len(pending), 1)
            record = parse_session_comment(f.comment["body"], identity=IDENTITY)
            packet = root / "packet.json"
            output = root / "result.json"
            child = root / "child"
            child.mkdir()
            packet.write_text(
                json.dumps(
                    {
                        "root": str(f.root),
                        "policy": f.policy,
                        "config": f.config_bytes.hex(),
                        "diff": DELETE,
                        "comments": [],
                        "remote": {
                            k: getattr(f, k)
                            for k in (
                                "comment",
                                "extra_comments",
                                "reviews",
                                "inline",
                                "check_runs",
                                "trace",
                                "threads",
                            )
                        },
                    }
                )
            )
            done = subprocess.run(
                [sys.executable, "-c", CHILD, str(packet), str(child), str(output)],
                capture_output=True,
                text=True,
                env=subprocess_environment(),
                timeout=90,
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            result = json.loads(output.read_text())
            self.assertEqual(result["code"], 1, result["error"])
            self.assertRegex(result["error"], r"pending|uncertain|unconfirmed")
            self.assertEqual(result["reviews"], [])
            self.assertEqual(result["threads"], [])
            self.assertEqual(result["provider_calls"], 0)
            self.assertEqual(
                result["record"]["finding_lifecycle"], dict(record.finding_lifecycle)
            )
