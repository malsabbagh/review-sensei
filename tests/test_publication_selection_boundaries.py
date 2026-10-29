"""Inactive selection never gains native write or transition authority."""

import copy
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

import test_publication_selection_handoff as handoff_tests
from review_sensei_github.partitioned_publication import publish_partitioned
from review_sensei_github.publication import ReviewPublisher

from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewResult
from review_sensei.publication_selection import (
    PublicationSelectionHandoff,
    PublishedSlot,
    freeze_publication_selection,
    validate_publication_selection_source,
)
from review_sensei.schemas import validate_public_document


class InactivePublicationBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = handoff_tests.PublicationSelectionHandoffTests()
        self.fixture.setUp()

    def test_both_publisher_entry_points_refuse_before_http_or_host_effects(self):
        f = self.fixture
        for rows, slots in ((f.rows, f.slots), (f.rows[:1], ()), ((), ())):
            source = f.source_for(rows)
            selected = freeze_publication_selection(
                source,
                candidates=rows,
                slots=slots,
                max_active_findings=2,
                policy=f.policy,
            )
            cold = ReviewResult.from_dict(selected.to_dict())
            http, host = Mock(), Mock()
            publisher = ReviewPublisher(http=http)
            with (
                self.subTest(rows=len(rows)),
                self.assertRaisesRegex(ReviewInputError, "inactive"),
            ):
                publisher.publish(
                    token="synthetic",
                    repository="owner/repo",
                    repository_id=1,
                    pull_request=1,
                    head_sha="a" * 40,
                    base_branch="main",
                    base_sha="b" * 40,
                    result=cold,
                    diff="",
                    app_slug="app[bot]",
                )
            with self.assertRaisesRegex(ReviewInputError, "inactive"):
                publisher.publish_partitioned(
                    token="synthetic",
                    host=host,
                    result=cold,
                    diff="",
                    configuration_context={},
                    convergence_policy=f.policy,
                    app_slug="app[bot]",
                )
            with self.assertRaisesRegex(ReviewInputError, "inactive"):
                publish_partitioned(
                    publisher,
                    token="synthetic",
                    host=host,
                    result=cold,
                    diff="",
                    configuration_context={},
                    convergence_policy=f.policy,
                    app_slug="app[bot]",
                )
            self.assertEqual(http.mock_calls, [])
            self.assertEqual(host.mock_calls, [])

    def test_changed_admission_criteria_fail_even_with_identical_selected_comments(
        self,
    ):
        f = self.fixture
        selected = f.freeze()
        changed = (replace(f.rows[0], evidence="supported-source"), *f.rows[1:])
        self.assertEqual(f.freeze(candidates=changed).comments, selected.comments)
        with self.assertRaisesRegex(ReviewInputError, "criteria changed"):
            validate_publication_selection_source(
                f.source,
                selected.publication_selection,
                candidates=changed,
                slots=f.slots,
                policy=f.policy,
            )
        reordered = f.freeze(
            candidates=tuple(reversed(f.rows)), slots=tuple(reversed(f.slots))
        )
        self.assertEqual(reordered, selected)

    def test_contingent_intent_remains_inactive_and_keeps_reservation(self):
        f = self.fixture
        rows = (replace(f.rows[1], existing_target="contingent"),)
        slots = (PublishedSlot("contingent", "contingent", "e" * 64),)
        result = f.freeze(
            source=f.source_for(rows),
            candidates=rows,
            slots=slots,
            max_active_findings=1,
        )
        action = result.publication_selection.actions[0]
        self.assertEqual((action.action, action.consumes_slot), ("reopen", False))
        result.publication_selection.require_occupancy(slots)
        with self.assertRaises(ReviewInputError):
            result.publication_selection.require_occupancy(
                (replace(slots[0], state="released"),)
            )
        with self.assertRaisesRegex(ReviewInputError, "inactive"):
            publish_partitioned(
                None,
                token="synthetic",
                host=None,
                result=result,
                diff="",
                configuration_context={},
                convergence_policy=f.policy,
                app_slug="app[bot]",
            )

    def test_closed_schema_and_decoder_reject_malformed_action_state(self):
        document = self.fixture.freeze().publication_selection.to_document()
        mutations = (
            lambda d: d.update(criteria_sha256=[]),
            lambda d: d["actions"][1].update(target_state=[]),
            lambda d: d["actions"][1].update(target_state="uncertain"),
            lambda d: d["actions"][1].update(action="create"),
            lambda d: d["actions"][1].update(target_proof_sha256=None),
            lambda d: d["actions"][0].update(existing_target="invented"),
            lambda d: d["actions"][0].update(consumes_slot=False),
            lambda d: d.update(actions=d["actions"] * 251),
        )
        for mutate in mutations:
            changed = copy.deepcopy(document)
            mutate(changed)
            with self.subTest(mutate=mutate), self.assertRaises(ReviewInputError):
                PublicationSelectionHandoff.from_document(changed)
            with self.assertRaises(ReviewInputError):
                validate_public_document(changed, "publication-selection")

    def test_action_order_comment_body_and_admission_are_bound(self):
        result = self.fixture.freeze()
        for mutate in (
            lambda d: d["publication_selection"]["actions"].reverse(),
            lambda d: d["comments"][0].update(body="changed selected prose"),
            lambda d: d["publication_selection"].update(
                selected_result_sha256="f" * 64
            ),
        ):
            changed = copy.deepcopy(result.to_dict())
            mutate(changed)
            with self.assertRaises(ReviewInputError):
                ReviewResult.from_dict(changed)
        with self.assertRaises(ReviewInputError):
            self.fixture.freeze(
                candidates=(
                    replace(
                        self.fixture.rows[0],
                        comment=replace(
                            self.fixture.rows[0].comment, effective_blocking=False
                        ),
                        required=False,
                    ),
                    *self.fixture.rows[1:],
                )
            )

    def test_public_application_refuses_before_broker_ledger_or_publisher(self):
        from review_sensei_github.application import (
            GitHubApplication,
            GitHubWriteOptions,
        )

        broker, http, reviewer, ledger = Mock(), Mock(), Mock(), Mock()
        application = GitHubApplication(
            broker=broker, http=http, reviewer=reviewer, learner=Mock(), replier=Mock()
        )
        application.session_ledger = ledger
        with self.assertRaisesRegex(ReviewInputError, "inactive"):
            application.publish_review(
                options=GitHubWriteOptions(github_writes=True, auto_review=True),
                oidc_token="synthetic",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha="a" * 40,
                base_branch="main",
                base_sha="b" * 40,
                result=self.fixture.freeze(),
                diff="",
                app_slug="app[bot]",
            )
        for dependency in (broker, http, reviewer, ledger):
            self.assertEqual(dependency.mock_calls, [])

    def test_real_cli_rejects_cold_handoff_before_acceptance_or_transport(self):
        from review_sensei_github.cli import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact, diff = root / "result.json", root / "diff.patch"
            artifact.write_text(
                json.dumps(self.fixture.freeze().to_dict()), encoding="utf-8"
            )
            diff.write_text(
                "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n",
                encoding="utf-8",
            )
            before = artifact.read_bytes()
            stderr, stdout = StringIO(), StringIO()
            with (
                patch.dict(
                    os.environ, {"GITHUB_TOKEN": "synthetic-metadata"}, clear=True
                ),
                patch(
                    "socket.socket.connect",
                    side_effect=AssertionError("transport forbidden"),
                ),
                redirect_stderr(stderr),
                redirect_stdout(stdout),
            ):
                code = main(
                    [
                        "publish",
                        "--result",
                        str(artifact),
                        "--diff",
                        str(diff),
                        "--repository",
                        "owner/repo",
                        "--repository-id",
                        "1",
                        "--pull-request",
                        "1",
                        "--head-sha",
                        "a" * 40,
                        "--base-sha",
                        "b" * 40,
                        "--base-branch",
                        "main",
                        "--allow-write",
                        "--enable-review",
                    ]
                )
            self.assertEqual(code, 1, stderr.getvalue())
            self.assertIn("selection is inactive", stderr.getvalue())
            self.assertEqual(artifact.read_bytes(), before)

    def test_malformed_direct_verdict_rejects_before_selection_validation(self):
        result = self.fixture.freeze()
        for invalid in (False, 1, {}, object()):
            with self.subTest(verdict=invalid), self.assertRaises(ReviewInputError):
                replace(result, review_verdict=invalid)
