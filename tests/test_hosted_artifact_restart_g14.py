"""Real hosted artifacts cross an interpreter boundary, not authority caches.

The command consumes native eligibility; interrupted analysis recovery consumes
retained analysis bytes after review.json is removed. Producers and consumers
run in separate interpreters. Only remote JSON and ordinary fixture files cross
that boundary, never a caller, publisher, manifest factory or authority cache.
All routes use the original 512-call configuration.
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

from fake_github_http import json_response
from review_sensei_github.publication import parse_finding_index
from review_sensei_github.session_ledger import parse_session_comment

from review_sensei.api_work import ApiRunAccounting
from review_sensei.errors import ReviewInputError
from tests.test_action_run import HEAD, IDENTITY, CommandEntryFixture
from tests.test_finding_dictionary_restart import FIELDS, save_remote


class _HostedRestartFixture(CommandEntryFixture):
    def __init__(self, directory, *, limit=512, broker_known=False):
        super().__init__(directory, limit=limit)
        self.broker_known = broker_known

    def _broker_response(self, request):
        response = super()._broker_response(request)
        if self.broker_known:
            payload = json.loads(response.body)
            if "session_state" in payload:
                payload["session_state"] = "known"
                return json_response(payload)
        return response


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
        fixture = _HostedRestartFixture(
            fresh,
            limit=512,
            broker_known=bool(saved.get("broker_known")),
        )
        for name in FIELDS:
            setattr(fixture, name, saved[name])
        fixture.root = fixture.directory = Path(saved["fixture_root"])
        fixture.extra_comments = {int(k): v for k, v in fixture.extra_comments.items()}
        fixture.past_jobs = {int(k): v for k, v in fixture.past_jobs.items()}
        fixture.resolved_threads = set(saved["resolved_threads"])
        for opt in ("put_fault", "action_fault", "archive_bad", "fault"):
            if opt in saved:
                setattr(fixture, opt, saved[opt])
        if review:
            if saved.get("next_run"):
                fixture.next_run()
            else:
                fixture.attempt = 2
            code = fixture.invoke()
        elif saved.get("command_attempt") == 2:
            fixture.attempt = 2
            code = fixture.invoke_command()
        else:
            fixture.SOURCE_ID += 1
            fixture.select_command((root / "command.txt").read_text(), new_run=True)
            code = fixture.invoke_command()
        save_remote(fixture, root / "after.json")
        try:
            actual_limit = fixture.accounted.accounting.configuration.limit
        except (AttributeError, ReviewInputError):
            actual_limit = fixture.limit
        (root / "outcome.json").write_text(
            json.dumps(
                {
                    "code": code,
                    "stdout": fixture.stdout,
                    "error": fixture.error,
                    "trace": fixture.trace,
                    "limit": actual_limit,
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

    def test_session_marker_mutations_hold_and_fail_closed_after_restart(self):
        for mode in ("deleted", "edited", "duplicated", "foreign", "stale"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.phase(root, "produce")
                before = json.loads((root / "remote.json").read_text())
                if mode == "deleted":
                    before["comment"] = None
                    before["broker_known"] = True
                elif mode == "edited":
                    before["comment"]["body"] = (
                        before["comment"]["body"][:-10] + "tampered -->"
                    )
                elif mode == "duplicated":
                    before["extra_comments"]["999001"] = {
                        **before["comment"],
                        "id": 999001,
                    }
                elif mode == "foreign":
                    before["comment"]["user"] = {
                        "id": 321,
                        "login": "human",
                        "type": "User",
                    }
                else:
                    alternate = root / "alternate"
                    alternate.mkdir()
                    self.phase(alternate, "alternate")
                    replacement = json.loads((alternate / "remote.json").read_text())
                    before["comment"]["body"] = replacement["comment"]["body"]
                (root / "remote.json").write_text(json.dumps(before))
                self.phase(root, "consume")
                outcome = json.loads((root / "outcome.json").read_text())
                after = json.loads((root / "after.json").read_text())
                if mode == "stale":
                    self.assertTrue(
                        outcome["code"] != 0
                        or "command finalization capacity unavailable"
                        in outcome["stdout"],
                        outcome,
                    )
                else:
                    self.assertNotEqual(outcome["code"], 0, outcome)
                self.assertEqual(outcome["limit"], 512)
                self.assertEqual(after["provider_calls"], 1)
                self.assertEqual(
                    after["resolved_threads"], before["resolved_threads"], outcome
                )
                self.assertEqual(after["reviews"], before["reviews"], outcome)
                self.assertFalse(
                    any(r["state"] == "APPROVED" for r in after["reviews"])
                )

    def test_duplicate_and_foreign_projections_never_grant_approval_or_resolve_threads(
        self,
    ):
        for mode in (
            "duplicate-review",
            "foreign-review",
            "duplicate-inline",
            "foreign-inline",
            "duplicate-check",
            "foreign-check",
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.phase(root, "produce")
                before = json.loads((root / "remote.json").read_text())
                (root / "command.txt").write_text("@reviewsensei verify")
                if mode == "duplicate-review":
                    dup = {**before["reviews"][-1], "id": 8801}
                    before["reviews"].append(dup)
                elif mode == "foreign-review":
                    before["reviews"].append(
                        {
                            **before["reviews"][-1],
                            "id": 8802,
                            "state": "APPROVED",
                            "user": {"id": 321, "login": "human", "type": "User"},
                        }
                    )
                elif mode == "duplicate-inline":
                    before["inline"].append({**before["inline"][0], "id": 8803})
                elif mode == "foreign-inline":
                    before["inline"][0]["user"] = {
                        "id": 321,
                        "login": "human",
                        "type": "User",
                    }
                    before["threads"][0]["comments"]["nodes"][0]["author"] = {
                        "login": "human",
                        "__typename": "User",
                    }
                elif mode == "duplicate-check":
                    before["check_runs"].append(
                        {
                            **before["check_runs"][0],
                            "id": 8804,
                            "conclusion": "success",
                        }
                    )
                else:
                    before["check_runs"].append(
                        {
                            **before["check_runs"][0],
                            "id": 8805,
                            "app": {"id": 888, "slug": "other-app"},
                            "conclusion": "success",
                        }
                    )
                (root / "remote.json").write_text(json.dumps(before))
                self.phase(root, "consume")
                outcome = json.loads((root / "outcome.json").read_text())
                after = json.loads((root / "after.json").read_text())
                self.assertEqual(outcome["limit"], 512)
                self.assertEqual(after["provider_calls"], 1)
                self.assertEqual(
                    after["resolved_threads"], before["resolved_threads"], outcome
                )
                self.assertFalse(
                    any(
                        r["state"] == "APPROVED" and r["user"]["type"] == "Bot"
                        for r in after["reviews"]
                    ),
                    outcome,
                )

    def test_archive_chain_mutations_fail_closed_in_fresh_process(self):
        for mode in ("deleted", "edited", "foreign", "truncated"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.phase(root, "produce")
                before = json.loads((root / "remote.json").read_text())
                # Start Run 2 cleanly up through rollover/archiving of Run 1, then pause
                # with a lost closure/business response while referencing previous_record.
                before["action_fault"] = "business-absent"
                (root / "remote.json").write_text(json.dumps(before))
                self.phase(root, "consume")
                archived = json.loads((root / "after.json").read_text())
                mid_outcome = json.loads((root / "outcome.json").read_text())
                self.assertNotEqual(mid_outcome["code"], 0, mid_outcome)
                mid_rec = parse_session_comment(
                    archived["comment"]["body"], identity=IDENTITY
                )
                self.assertIsNotNone(mid_rec.api_run_accounting["previous_record"])
                archive_keys = [
                    k
                    for k, v in archived["extra_comments"].items()
                    if v["body"].startswith("ReviewSensei immutable evidence part v1")
                ]
                self.assertEqual(len(archive_keys), 1, archived["extra_comments"])
                akey = archive_keys[0]
                if mode == "deleted":
                    del archived["extra_comments"][akey]
                elif mode == "edited":
                    archived["extra_comments"][akey]["body"] = archived[
                        "extra_comments"
                    ][akey]["body"].replace(
                        '"binding_sha256":"', '"binding_sha256":"f', 1
                    )
                elif mode == "foreign":
                    archived["extra_comments"][akey]["user"] = {
                        "id": 321,
                        "login": "human",
                        "type": "User",
                    }
                else:
                    archived["extra_comments"][akey]["body"] = archived[
                        "extra_comments"
                    ][akey]["body"][:40]
                archived["trace"] = []
                archived["command_attempt"] = 2
                (root / "remote.json").write_text(json.dumps(archived))
                self.phase(root, "consume")
                outcome = json.loads((root / "outcome.json").read_text())
                after = json.loads((root / "after.json").read_text())
                self.assertNotEqual(outcome["code"], 0, outcome)
                self.assertEqual(outcome["limit"], 512)
                self.assertEqual(after["provider_calls"], 1)
                self.assertFalse(
                    any(r["state"] == "APPROVED" for r in after["reviews"])
                )
                self.assertEqual(
                    after["resolved_threads"], archived["resolved_threads"], outcome
                )

    def test_response_loss_plus_projection_mutation_preserves_uncertainty_across_restart(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.phase(root, "produce")
            before = json.loads((root / "remote.json").read_text())
            # Simulate response loss after PUT stores on the review projection so the
            # action remains pending with uncertain > 0, while the projection summary
            # is mutated before a fresh-process recovery run.
            before["put_fault"] = "stored"
            (root / "remote.json").write_text(json.dumps(before))
            self.phase(root, "consume")
            mid = json.loads((root / "after.json").read_text())
            mid_outcome = json.loads((root / "outcome.json").read_text())
            self.assertNotEqual(mid_outcome["code"], 0, mid_outcome)
            mid_rec = parse_session_comment(mid["comment"]["body"], identity=IDENTITY)
            self.assertEqual(mid_rec.api_run_accounting["action"]["phase"], "pending")
            self.assertGreaterEqual(
                ApiRunAccounting.from_document(mid_rec.api_run_accounting).uncertain,
                1,
            )
            self.assertFalse(any(r["state"] == "APPROVED" for r in mid["reviews"]))

            # Mutate the review projection between lost response and cold successor restart:
            mid["reviews"][-1]["body"] += "\nHuman edited review during response loss."
            mid["reviews"][-1]["edited_by_human"] = True
            mid["command_attempt"] = 2
            (root / "remote.json").write_text(json.dumps(mid))
            self.phase(root, "consume")
            after = json.loads((root / "after.json").read_text())
            after_outcome = json.loads((root / "outcome.json").read_text())
            after_rec = parse_session_comment(
                after["comment"]["body"], identity=IDENTITY
            )
            self.assertNotEqual(after_outcome["code"], 0, after_outcome)
            self.assertGreaterEqual(
                ApiRunAccounting.from_document(after_rec.api_run_accounting).uncertain,
                1,
            )
            self.assertEqual(after_outcome["limit"], 512)
            self.assertEqual(after["provider_calls"], 1)
            self.assertFalse(
                any(r["state"] == "APPROVED" for r in after["reviews"]), after_outcome
            )
            self.assertEqual(
                after["resolved_threads"], before["resolved_threads"], after_outcome
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
