"""Published-tail origins preserve every independent historical assessment."""

import os
import subprocess
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from test_quarantine_assessments import origins_value
from test_quarantine_evidence import git, original_objects
from test_quarantine_lineage import decision
from test_review_run import EntryFixture

from review_sensei.errors import ReviewInputError
from review_sensei.quarantine_assessments import (
    assessment_view,
    origin_scope,
    pending_assessments,
    validate_origins,
)


def published_origins():
    origins, inventories = origins_value()
    origins["schema_version"] = "quarantine-published-origins-v1"
    updated = {}
    for origin, kind in zip(
        origins["origins"], ("published-current", "published-earlier"), strict=True
    ):
        inventory = inventories[origin["scope_sha256"]]
        origin["kind"] = kind
        origin["scope_sha256"] = origin_scope(origin)
        updated[origin["scope_sha256"]] = inventory
    origins["origins"].sort(key=lambda o: o["scope_sha256"])
    return origins, updated


class PublishedTailAssessmentTests(unittest.TestCase):
    def test_two_real_published_origins_retain_five_and_nine_without_completion(self):
        from review_sensei_github.api_run_requests import ApiRunRequestTransport
        from review_sensei_github.quarantine_evidence import (
            CurrentReadScope,
            read_published_tail_sources,
        )
        from review_sensei_github.session_ledger import parse_session_comment

        with tempfile.TemporaryDirectory() as directory:
            fixture = EntryFixture(directory)
            original_objects(fixture)
            fixture.provider_comments = [
                {
                    "path": "src/app.py",
                    "line": 2,
                    "side": "RIGHT",
                    "body": f"Earlier independent assessment {i} requires a human decision.",
                    "severity": "high",
                    "blocking": False,
                    "needs_human": True,
                }
                for i in range(5)
            ]
            self.assertEqual(fixture.invoke(human=True), 0, fixture.error)
            old_head = fixture.live_head
            fixture.next_run()
            git(fixture.root, "checkout", "--detach", old_head)
            (fixture.root / "src/app.py").write_text("keep\nchange\nmore\n")
            git(fixture.root, "add", "src/app.py")
            git(fixture.root, "commit", "-m", "second reviewed head")
            fixture.live_head = fixture.requested_head = git(
                fixture.root, "rev-parse", "HEAD"
            )
            diff = subprocess.check_output(
                [
                    "git",
                    "-C",
                    str(fixture.root),
                    "diff",
                    fixture.live_base + "..." + fixture.live_head,
                ]
            )
            (fixture.root / "review.patch").write_bytes(diff)
            git(fixture.root, "checkout", "--detach", fixture.policy)
            fixture.provider_comments = [
                {
                    "path": "src/app.py",
                    "line": 3,
                    "side": "RIGHT",
                    "body": f"Current independent assessment {i} requires a human decision.",
                    "severity": "high",
                    "blocking": False,
                    "needs_human": True,
                }
                for i in range(9)
            ]
            with patch.object(
                ApiRunRequestTransport,
                "finish_review_run",
                side_effect=KeyboardInterrupt,
            ):
                self.assertEqual(fixture.invoke(human=True), 130, fixture.error)
            previous = parse_session_comment(
                fixture.comment["body"], identity=fixture.selected.identity
            )
            self.assertEqual(previous.transaction.phase, "publication_succeeded")
            self.assertIsNone(previous.api_run_accounting["terminal_transaction"])
            before = deepcopy(fixture.comment)
            trace = len(fixture.trace)
            scope = CurrentReadScope(
                fixture.selected.ledger,
                previous.identity(),
                fixture.live_base,
                fixture.live_head,
                "synthetic-workflow-read",
            )
            cwd = Path.cwd()
            try:
                os.chdir(fixture.root)
                sources = read_published_tail_sources(
                    ledger=fixture.selected.ledger,
                    previous=previous,
                    current_scope=scope,
                )
            finally:
                os.chdir(cwd)
            self.assertEqual(len(sources["published-current"][1]["assessments"]), 9)
            self.assertEqual(len(sources["published-earlier"][1]["assessments"]), 5)
            self.assertEqual(fixture.comment, before)
            self.assertTrue(
                all(method == "GET" for _, method, _ in fixture.trace[trace:])
            )

    def test_same_instance_in_two_publications_requires_two_exact_decisions(self):
        origins, inventories = published_origins()
        scopes = [o["scope_sha256"] for o in origins["origins"]]
        state = {
            "schema_version": "quarantine-assessment-state-v1",
            "scope_sha256": "0" * 64,
            "settlements": [],
        }
        self.assertEqual(
            pending_assessments(origins, inventories, state, scope="0" * 64),
            tuple((s, "1" * 64) for s in scopes),
        )
        state["settlements"].append(decision(scopes[0]))
        view = assessment_view(
            origins, inventories, state, scope="0" * 64, fresh_human_pending=True
        )
        self.assertEqual(
            view["pending"], [{"origin_scope_sha256": scopes[1], "instance": "1" * 64}]
        )
        self.assertTrue(view["fresh_human_pending"])

    def test_publication_variant_cannot_drop_or_mix_either_origin(self):
        origins, _ = published_origins()
        for changes in ("drop", "mix", "legacy"):
            bad = deepcopy(origins)
            if changes == "drop":
                bad["origins"].pop()
            elif changes == "mix":
                bad["origins"][0]["kind"] = "retained-result"
            else:
                bad["schema_version"] = "quarantine-assessment-origins-v1"
            with self.subTest(changes=changes), self.assertRaises(ReviewInputError):
                validate_origins(bad)
