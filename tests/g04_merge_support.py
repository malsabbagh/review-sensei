"""Test-only G04 hosted route with real local Git acquisition and fake HTTP."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from fake_github_http import json_response
from test_review_run import EntryFixture

ROWS = []


def git(root, *args, check=True, input=None):
    result = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        input=input,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode:
        raise AssertionError(f"synthetic git {args[0]} refused: {result.stderr}")
    return result


class G04MergeFixture(EntryFixture):
    def __init__(self, directory):
        super().__init__(directory)
        # Keep the literal diff oracle independent of platform newline conversion
        # and user-level Git formatting defaults. Production prepare_diff runs Git.
        for key, value in (
            ("core.autocrlf", "false"),
            ("core.abbrev", "7"),
            ("diff.context", "3"),
            ("diff.noprefix", "false"),
            ("diff.mnemonicprefix", "false"),
        ):
            git(self.root, "config", "--local", key, value)
        app = self.root / "src/app.py"
        app.parent.mkdir()
        app.write_bytes(b"value = 0\nkeep = True\n")
        git(self.root, "add", "src/app.py")
        git(self.root, "commit", "-m", "common synthetic source")
        self.common = git(self.root, "rev-parse", "HEAD").stdout.strip()
        app.write_bytes(b"value = 1\nkeep = True\n")
        git(self.root, "commit", "-am", "reviewed synthetic branch")
        self.original_head = git(self.root, "rev-parse", "HEAD").stdout.strip()
        git(self.root, "checkout", "--detach", self.common)
        app.write_bytes(b"value = 2\nkeep = True\n")
        git(self.root, "commit", "-am", "conflicting synthetic base")
        self.conflict_base = git(self.root, "rev-parse", "HEAD").stdout.strip()
        # Two-parent resolution makes conflict_base an ancestor, without merging
        # any user's repository or executing head code. Both commits are local fixtures.
        tree = git(
            self.root, "rev-parse", self.original_head + "^{tree}"
        ).stdout.strip()
        self.resolved_head = git(
            self.root,
            "commit-tree",
            tree,
            "-p",
            self.original_head,
            "-p",
            self.conflict_base,
            input="Resolve synthetic conflict\n",
        ).stdout.strip()
        # Preserve the real bootstrap policy-checkout fence after seeding objects.
        git(self.root, "checkout", "--detach", self.policy)
        self.live_base = self.requested_base = self.common
        self.live_head = self.requested_head = self.original_head
        self.merge_fields = {"mergeable": True, "mergeable_state": "clean"}
        self.pr_overrides = {}
        self.source_fault = None
        self.pull_reads = []
        self.acquisitions = []
        self.before_acquisition = None
        self.broker_reads = []
        self.review_payloads = []
        self.analysis_diffs = []
        self.provider_has_exact_diff = []
        self.preflight_results = []

    def external(self, request, timeout):
        path = urlsplit(request.full_url).path
        if urlsplit(request.full_url).hostname == "broker.github.test":
            self.broker_reads.append(request.get_method())
        if path.endswith("/pulls/7/reviews") and request.get_method() == "POST":
            self.review_payloads.append(json.loads(request.data))
        response = super().external(request, timeout)
        if path.endswith("/pulls/7"):
            value = json.loads(response.read(524288))
            value.update(self.merge_fields)
            value.update(self.pr_overrides)
            if self.source_fault == "repository_id":
                value["base"]["repo"]["id"] = 987
            elif self.source_fault == "fork":
                value["head"]["repo"]["fork"] = True
            self.pull_reads.append(
                {
                    "base": value["base"]["sha"],
                    "head": value["head"]["sha"],
                    "draft": value.get("draft"),
                    "state": value.get("state"),
                    **self.merge_fields,
                }
            )
            return json_response(value)
        return response

    def conflicts(self, base=None, head=None):
        result = git(
            self.root,
            "merge-tree",
            "--write-tree",
            base or self.live_base,
            head or self.live_head,
            check=False,
        )
        if result.returncode not in (0, 1):
            raise AssertionError(result.stderr)
        return result.returncode == 1

    def invoke_fresh(self):
        import socket
        from unittest.mock import patch

        from review_sensei_github import cli as hosted_cli
        from review_sensei_github import review_run

        from review_sensei import workflow

        original_main = hosted_cli.main
        original_prepare = review_run.prepare_diff
        from review_sensei_github.publication import ReviewPublisher

        from review_sensei.service import ReviewService

        original_run = ReviewService.run
        original_preflight = ReviewPublisher._preflight_pr

        def run(service, request, **kwargs):
            self.analysis_diffs.append(request.diff)
            original_complete = service.provider.complete

            def complete(provider_request):
                self.provider_has_exact_diff.append(
                    request.diff in provider_request.prompt
                )
                return original_complete(provider_request)

            with patch.object(service.provider, "complete", complete):
                return original_run(service, request, **kwargs)

        def preflight(publisher, *args, **kwargs):
            result = original_preflight(publisher, *args, **kwargs)
            self.preflight_results.append(
                result.result.status if result.result else "eligible"
            )
            return result

        def main(argv):
            argv = list(argv)
            index = argv.index("--diff")
            del argv[index : index + 2]
            return original_main(argv)

        def prepare(**kwargs):
            if self.before_acquisition:
                self.before_acquisition()
            row = {
                "base": kwargs["base_ref"],
                "head": kwargs["head_ref"],
                "reported_mergeability": dict(self.merge_fields),
                "actual_conflict": self.conflicts(
                    kwargs["base_ref"], kwargs["head_ref"]
                ),
            }
            result = original_prepare(**kwargs)
            content = Path(kwargs["output"]).read_bytes()
            row["diff_sha256"] = hashlib.sha256(content).hexdigest()
            row["diff_bytes"] = len(content)
            old_value = 2 if kwargs["head_ref"] == self.resolved_head else 0
            old_blob = f"value = {old_value}\nkeep = True\n".encode()
            new_blob = b"value = 1\nkeep = True\n"

            def oid(blob):
                return hashlib.sha1(
                    b"blob " + str(len(blob)).encode() + b"\0" + blob
                ).hexdigest()[:7]

            expected = (
                f"diff --git a/src/app.py b/src/app.py\nindex {oid(old_blob)}..{oid(new_blob)} 100644\n"
                f"--- a/src/app.py\n+++ b/src/app.py\n@@ -1,2 +1,2 @@\n-value = {old_value}\n+value = 1\n keep = True\n"
            ).encode()
            assert content == expected, (
                "fresh acquisition did not replace the preexisting patch with the exact branch diff"
            )
            row["expected_diff_sha256"] = hashlib.sha256(expected).hexdigest()
            self.acquisitions.append(row)
            return result

        with (
            patch.object(
                socket,
                "create_connection",
                side_effect=AssertionError("live socket forbidden"),
            ),
            patch.object(
                workflow,
                "_fetch_ref",
                side_effect=AssertionError("Git remote fetch forbidden"),
            ),
            patch.object(hosted_cli, "main", main),
            patch.object(ReviewService, "run", run),
            patch.object(ReviewPublisher, "_preflight_pr", preflight),
            patch.object(review_run, "prepare_diff", prepare),
        ):
            return self.invoke()

    def observation(self, *, case, code):
        from review_sensei_github.session_ledger import parse_session_comment
        from test_api_work_persistence import IDENTITY

        ledger = (
            parse_session_comment(self.comment["body"], identity=IDENTITY)
            if self.comment
            else None
        )
        from review_sensei.errors import ReviewInputError

        try:
            accounting = self.accounted.accounting if self.accounted else None
        except ReviewInputError:
            accounting = None
        row = {
            "case": case,
            "event_name": self.event_name,
            "accounting_available": accounting is not None,
            "analyzed_diff_sha256": [
                hashlib.sha256(d.encode()).hexdigest() for d in self.analysis_diffs
            ],
            "provider_has_exact_diff": self.provider_has_exact_diff,
            "publisher_preflight": self.preflight_results,
            "exit": code,
            "error": self.error.strip(),
            "provider_calls": self.provider_calls,
            "acquisitions": self.acquisitions,
            "pull_reads": self.pull_reads,
            "review_events": [p["event"] for p in self.review_payloads],
            "review_commits": [p["commit_id"] for p in self.review_payloads],
            "phase": ledger.transaction.phase
            if ledger and ledger.transaction
            else None,
            "terminal_transaction": accounting.terminal_transaction
            if accounting
            else None,
            "api_limit": accounting.configuration.limit if accounting else None,
            "api_observations": len(self.accounted.observations)
            if self.accounted
            else 0,
            "final_actual_conflict": self.conflicts(),
        }
        ROWS.append(row)
        return row
