"""Real hosted artifacts cross an interpreter boundary, not authority caches.

The command consumes native eligibility; interrupted analysis recovery consumes
retained analysis bytes after review.json is removed. Producers and consumers
run in separate interpreters. Only remote JSON and ordinary fixture files cross
that boundary, never a caller, publisher, manifest factory or authority cache.
All routes use the original 512-call configuration. This does not qualify
archive-chain mutation or claim unused historical analysis gates a command.
"""

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from review_sensei_github.publication import parse_finding_index
from review_sensei_github.session_ledger import parse_session_comment

from review_sensei.api_work import ApiRunAccounting
from tests.test_action_run import HEAD, IDENTITY, CommandEntryFixture
from tests.test_finding_dictionary_restart import FIELDS, save_remote


def produce(root, *, alternate=False, pending=False):
    (root / "producer").mkdir()
    fixture = CommandEntryFixture(root / "producer", limit=512)
    fixture.provider_comments = [
        {
            "path": "src/app.py",
            "line": 2,
            "side": "RIGHT",
            "body": f"Confirm {'alternate' if alternate else 'interface'} assumption {n}.",
            "severity": "high",
            "blocking": False,
            "needs_human": True,
        }
        for n in range(2)
    ]
    if pending:

        def interrupted():
            raise KeyboardInterrupt()

        fixture.before_publication = interrupted
        assert fixture.invoke() == 130, fixture.error
        (fixture.root / "review.json").unlink()
        save_remote(fixture, root / "remote.json")
        return
    assert fixture.invoke() == 0, fixture.error
    assert fixture.reviews, fixture.stdout + fixture.error
    index = parse_finding_index(
        fixture.reviews[-1]["body"], repository_id=123, pull_request=7, head_sha=HEAD
    )
    (root / "command.txt").write_text(
        "@reviewsensei override "
        + " ".join("RS-" + e.instance[:6].upper() for e in index.entries)
        + " acceptable risk"
    )
    save_remote(fixture, root / "remote.json")


def consume(root, *, review=False):
    saved = json.loads((root / "remote.json").read_text())
    with tempfile.TemporaryDirectory() as fresh:
        fixture = CommandEntryFixture(fresh, limit=512)
        for name in FIELDS:
            setattr(fixture, name, saved[name])
        fixture.root = fixture.directory = Path(saved["fixture_root"])
        fixture.extra_comments = {int(k): v for k, v in fixture.extra_comments.items()}
        fixture.past_jobs = {int(k): v for k, v in fixture.past_jobs.items()}
        fixture.resolved_threads = set(saved["resolved_threads"])
        if review:
            fixture.attempt = 2
            code = fixture.invoke()
        else:
            fixture.SOURCE_ID += 1
            fixture.select_command((root / "command.txt").read_text(), new_run=True)
            code = fixture.invoke_command()
        save_remote(fixture, root / "after.json")
        (root / "outcome.json").write_text(
            json.dumps(
                {
                    "code": code,
                    "stdout": fixture.stdout,
                    "error": fixture.error,
                    "trace": fixture.trace,
                    "limit": fixture.accounted.accounting.configuration.limit,
                }
            )
        )


def artifact_parts(remote):
    parts = {}
    for key, item in remote["extra_comments"].items():
        if item["body"].startswith("<!-- review-sensei-result -->"):
            envelope = json.loads(
                item["body"].split("```json\n", 1)[1].split("\n```", 1)[0]
            )
            decoded = json.loads(base64.b64decode(envelope["data"]))
            parts["context" if "human_inventory" in decoded else "result"] = key
    return parts


class HostedArtifactRestartG14Tests(unittest.TestCase):
    def phase(self, root, mode):
        environment = dict(os.environ)
        repo = Path(__file__).resolve().parents[1]
        environment["PYTHONPATH"] = os.pathsep.join(
            map(
                str,
                (
                    repo,
                    repo / "src",
                    repo / "packages/python/review-sensei-github/src",
                    repo / "tests",
                ),
            )
        )
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "tests.test_hosted_artifact_restart_g14",
                mode,
                str(root),
            ],
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unchanged_actual_eligibility_context_finishes_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.phase(root, "produce")
            before = json.loads((root / "remote.json").read_text())
            self.assertEqual(set(artifact_parts(before)), {"result"})
            self.phase(root, "consume")
            outcome = json.loads((root / "outcome.json").read_text())
            self.assertEqual(outcome["code"], 0, outcome)
            after = json.loads((root / "after.json").read_text())
            self.assertEqual(len(after["resolved_threads"]), 2, outcome)
            self.assertEqual(sum(r["state"] == "APPROVED" for r in after["reviews"]), 1)

    def test_actual_eligibility_artifact_mutations_hold_original_inventory(self):
        for mode in ("changed", "partial", "missing", "foreign", "replaced"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.phase(root, "produce")
                before = json.loads((root / "remote.json").read_text())
                original = parse_session_comment(
                    before["comment"]["body"], identity=IDENTITY
                )
                target = before["reviews"][-1]
                marker = re.search(
                    r"<!-- reviewsensei:eligibility:v1 ([A-Za-z0-9_=-]+) -->",
                    target["body"],
                )
                self.assertIsNotNone(marker)
                if mode == "changed":
                    value = json.loads(base64.urlsafe_b64decode(marker.group(1)))
                    value["result_digest"] = "f" * 64
                    encoded = base64.urlsafe_b64encode(
                        json.dumps(value).encode()
                    ).decode()
                    target["body"] = target["body"].replace(marker.group(1), encoded)
                elif mode == "partial":
                    target["body"] = target["body"].replace(
                        marker.group(1), marker.group(1)[:20]
                    )
                elif mode == "missing":
                    target["body"] = target["body"].replace(marker.group(0), "")
                elif mode == "foreign":
                    target["user"] = {"id": 321, "login": "human", "type": "User"}
                else:
                    alternate = root / "alternate"
                    alternate.mkdir()
                    self.phase(alternate, "alternate")
                    replacement = json.loads((alternate / "remote.json").read_text())
                    target["body"] = replacement["reviews"][-1]["body"]
                (root / "remote.json").write_text(json.dumps(before))
                self.phase(root, "consume")
                outcome = json.loads((root / "outcome.json").read_text())
                after = json.loads((root / "after.json").read_text())
                self.assertEqual(
                    after["resolved_threads"], before["resolved_threads"], outcome
                )
                self.assertEqual(after["reviews"], before["reviews"], outcome)
                self.assertEqual(after["check_runs"], before["check_runs"], outcome)
                self.assertEqual(after["inline"], before["inline"], outcome)
                self.assertEqual(after["provider_calls"], 1)
                retained = parse_session_comment(
                    after["comment"]["body"], identity=IDENTITY
                )
                self.assertEqual(retained.transaction, original.transaction)
                self.assertEqual(retained.dispositions, original.dispositions)
                self.assertEqual(
                    retained.finding_lifecycle["manifest"],
                    original.finding_lifecycle["manifest"],
                )
                self.assertEqual(outcome["limit"], 512)

    def test_pending_analysis_recovery_uses_exact_remote_result_in_fresh_process(self):
        for mode in (
            "unchanged",
            "changed",
            "partial",
            "missing",
            "foreign",
            "replaced",
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.phase(root, "pending")
                before = json.loads((root / "remote.json").read_text())
                original = parse_session_comment(
                    before["comment"]["body"], identity=IDENTITY
                )
                self.assertEqual(original.transaction.phase, "publication_pending")
                self.assertFalse(
                    (Path(before["fixture_root"]) / "review.json").exists()
                )
                key = artifact_parts(before)["result"]
                target = before["extra_comments"][key]
                if mode == "changed":
                    target["body"] = target["body"].replace('"data":"', '"data":"AA', 1)
                elif mode == "partial":
                    target["body"] = target["body"][: len(target["body"]) // 2]
                elif mode == "missing":
                    del before["extra_comments"][key]
                elif mode == "foreign":
                    target["user"] = {"id": 321, "login": "human", "type": "User"}
                elif mode == "replaced":
                    alternate = root / "alternate"
                    alternate.mkdir()
                    self.phase(alternate, "alternate-pending")
                    replacement = json.loads((alternate / "remote.json").read_text())
                    target["body"] = replacement["extra_comments"][
                        artifact_parts(replacement)["result"]
                    ]["body"]
                (root / "remote.json").write_text(json.dumps(before))
                self.phase(root, "resume")
                outcome = json.loads((root / "outcome.json").read_text())
                after = json.loads((root / "after.json").read_text())
                retained = parse_session_comment(
                    after["comment"]["body"], identity=IDENTITY
                )
                self.assertEqual(after["provider_calls"], 1, outcome)
                old_accounting = ApiRunAccounting.from_document(
                    original.api_run_accounting
                )
                new_accounting = ApiRunAccounting.from_document(
                    retained.api_run_accounting
                )
                self.assertEqual(
                    new_accounting.blocks[: len(old_accounting.blocks)],
                    old_accounting.blocks,
                )
                self.assertEqual(
                    new_accounting.configuration, old_accounting.configuration
                )
                self.assertEqual(new_accounting.identity, old_accounting.identity)
                self.assertEqual(
                    retained.transaction.transaction_id,
                    original.transaction.transaction_id,
                )
                self.assertEqual(
                    retained.transaction.result_sha256,
                    original.transaction.result_sha256,
                )
                self.assertEqual(outcome["limit"], 512)
                if mode == "unchanged":
                    self.assertEqual(outcome["code"], 0, outcome)
                    self.assertEqual(
                        retained.transaction.phase, "publication_succeeded"
                    )
                    self.assertEqual(len(after["inline"]), 2)
                else:
                    self.assertNotEqual(outcome["code"], 0, outcome)
                    self.assertEqual(after["reviews"], before["reviews"], outcome)
                    self.assertEqual(after["inline"], before["inline"])
                    self.assertEqual(after["check_runs"], before["check_runs"])
                    self.assertEqual(retained.transaction, original.transaction)
                    if mode == "missing":
                        self.assertNotIn(key, after["extra_comments"])
                    else:
                        self.assertEqual(
                            after["extra_comments"][key], before["extra_comments"][key]
                        )


if __name__ == "__main__":
    if len(sys.argv) == 3:
        {
            "produce": produce,
            "alternate-pending": lambda root: produce(
                root, alternate=True, pending=True
            ),
            "pending": lambda root: produce(root, pending=True),
            "resume": lambda root: consume(root, review=True),
            "alternate": lambda root: produce(root, alternate=True),
            "consume": consume,
        }[sys.argv[1]](Path(sys.argv[2]))
    else:
        unittest.main()
