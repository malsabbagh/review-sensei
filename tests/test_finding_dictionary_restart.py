"""Hosted pending dictionary checkpoints survive an actual interpreter crash.

Only fake remote JSON and ordinary fixture files cross the process boundary.
No publisher, caller, plan or dictionary cache is serialized.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from review_sensei_github import ReviewPublisher
from review_sensei_github.finding_lifecycle import restore_finding_lifecycle
from review_sensei_github.publication import parse_finding_index

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.finding_lifecycle import decode_lifecycle_state
from review_sensei.finding_lifecycle_store import select_finding_lifecycle
from tests.test_action_run import HEAD, CommandEntryFixture

FIELDS = (
    "comment",
    "extra_comments",
    "reviews",
    "inline",
    "threads",
    "check_runs",
    "past_jobs",
    "run_id",
    "owner_job",
    "event_name",
    "command_active",
    "command_body",
    "source",
    "policy",
    "policy_ref",
    "live_head",
    "live_base",
    "requested_head",
    "requested_base",
    "SOURCE_ID",
    "provider_calls",
    "business_writes",
    "closure_writes",
    "review_updates",
    "review_put_attempts",
)


def save_remote(fixture, path):
    value = {name: getattr(fixture, name) for name in FIELDS}
    value["resolved_threads"] = sorted(fixture.resolved_threads)
    value["fixture_root"] = str(fixture.root)
    path.write_text(json.dumps(value), encoding="utf-8")


class CrashingFixture(CommandEntryFixture):
    crash_path = None

    def external(self, request, timeout):
        result = super().external(request, timeout)
        if (
            self.crash_path is not None
            and request.method == "POST"
            and urlsplit(request.full_url).path == "/graphql"
        ):
            payload = json.loads(request.data)
            if "resolveReviewThread" in payload.get("query", ""):
                save_remote(self, self.crash_path)
                os._exit(77)
        return result


def produce(directory):
    root = directory / "producer"
    root.mkdir()
    fixture = CrashingFixture(root)
    fixture.provider_comments = [
        {
            "path": "src/app.py",
            "line": 2,
            "side": "RIGHT",
            "body": f"Confirm interface assumption {i}.",
            "severity": "high",
            "blocking": False,
            "needs_human": True,
        }
        for i in range(20)
    ]
    assert fixture.invoke() == 0, fixture.error
    index = parse_finding_index(
        fixture.reviews[-1]["body"], repository_id=123, pull_request=7, head_sha=HEAD
    )
    fixture.SOURCE_ID += 1
    fixture.select_command(
        "@reviewsensei override "
        + " ".join("RS-" + item.instance[:6].upper() for item in index.entries[:10])
        + " acceptable risk",
        new_run=True,
    )
    fixture.crash_path = directory / "remote.json"
    code = fixture.invoke_command()
    raise AssertionError(("did not reach native crash", code, fixture.error))


def restore(directory):
    saved = json.loads((directory / "remote.json").read_text())
    with tempfile.TemporaryDirectory() as fresh:
        fixture = CommandEntryFixture(fresh)
        for name in FIELDS:
            setattr(fixture, name, saved[name])
        fixture.root = fixture.directory = Path(saved["fixture_root"])
        fixture.extra_comments = {
            int(key): value for key, value in fixture.extra_comments.items()
        }
        fixture.past_jobs = {
            int(key): value for key, value in fixture.past_jobs.items()
        }
        fixture.resolved_threads = set(saved["resolved_threads"])
        # A fresh interpreter is the original run's second attempt, not reuse
        # of its already consumed startup slot or a replacement allowance.
        fixture.attempt = 2
        observation = {}

        def after_bootstrap(run):
            selected, stored = select_finding_lifecycle(run.ledger, run.identity)
            observation["carrier"] = stored["schema_version"]
            observation["target_sha256"] = stored["sha256"]
            reviewer = ReviewPublisher(http=run.http)
            record, document = restore_finding_lifecycle(
                reviewer, run.ledger, run.identity, loaded=selected
            )
            observation["document"] = document
            observation["limit"] = run.caller.accounting.configuration.limit
            observation["action"] = record.api_run_accounting["action"]

        fixture.after_action_bootstrap = after_bootstrap
        code = fixture.invoke_command()
        observation.update(
            code=code,
            error=fixture.error,
            provider_calls=fixture.provider_calls,
            resolved_threads=sorted(fixture.resolved_threads),
            reviews=fixture.reviews,
            review_put_attempts=fixture.review_put_attempts,
            trace=fixture.trace,
        )
        (directory / "restored.json").write_text(json.dumps(observation))


class FindingDictionaryRestartTests(unittest.TestCase):
    def test_hosted_pending_checkpoint_cold_restore_preserves_unknown_without_repost(
        self,
    ):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            environment = dict(os.environ)
            paths = [Path(__file__).resolve().parents[1]]
            environment["PYTHONPATH"] = os.pathsep.join(
                str(path)
                for path in (
                    paths[0],
                    paths[0] / "src",
                    paths[0] / "packages/python/review-sensei-github/src",
                    paths[0] / "tests",
                )
            )
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            for name in (
                "REVIEWSENSEI_PROVIDER_MODE",
                "REVIEWSENSEI_CLOUD_MODEL",
                "REVIEWSENSEI_LOCAL_MODEL",
                "OLLAMA_MODEL",
            ):
                environment.pop(name, None)

            def worker(mode):
                return subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "tests.test_finding_dictionary_restart",
                        mode,
                        str(root),
                    ],
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=180,
                )

            crashed = worker("produce")
            self.assertEqual(crashed.returncode, 77, crashed.stderr)
            before = json.loads((root / "remote.json").read_text())
            self.assertEqual(len(before["resolved_threads"]), 1)
            cold = worker("restore")
            self.assertEqual(cold.returncode, 0, cold.stderr)
            after = json.loads((root / "restored.json").read_text())
            self.assertEqual(
                after.get("carrier"), "finding-lifecycle-packed-v4", after.get("error")
            )
            self.assertEqual(after["limit"], 1024)
            self.assertEqual(after["resolved_threads"], before["resolved_threads"])
            self.assertEqual(after["reviews"], before["reviews"])
            self.assertEqual(
                after["review_put_attempts"], before["review_put_attempts"]
            )
            self.assertEqual(after["provider_calls"], 1)
            self.assertEqual(
                hashlib.sha256(canonical_bytes(after["document"])).hexdigest(),
                after["target_sha256"],
            )
            restored = decode_lifecycle_state(after["document"]["lifecycle"])
            inventory = (
                restored.state.authoritative_decision_inventory
                if hasattr(restored.state, "authoritative_decision_inventory")
                else restored.state.inventory
            )
            self.assertEqual(len(inventory.obligations), 20)
            self.assertEqual(len(after["document"]["native_receipts"]), 20)
            self.assertTrue(
                any(
                    effect["status"] == "pending"
                    for effect in after["document"]["effects"]
                )
            )
            self.assertTrue(after["action"]["execution_started"])
            self.assertNotEqual(after["code"], 0)
            self.assertIn("action has started or uncertain caller work", after["error"])
            self.assertIn("prior charges preserved", after["error"])


if __name__ == "__main__":
    {"produce": produce, "restore": restore}[sys.argv[1]](Path(sys.argv[2]))
