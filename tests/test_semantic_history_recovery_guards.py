"""Security, schema, and snapshot guards for recovery disposition commands."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from review_sensei_github import action_run, review_run
from review_sensei_github.accepted_semantic_history import (
    _NON_ADVANCING_COMMAND_ACTIONS,
    _STATE_ADVANCING_COMMAND_ACTIONS,
    LIMIT,
    _target_allows_disposition_entry,
    append_history,
    capture_selected,
    guard_carried_successor,
    historical_snapshot,
    prepare_action,
    semantic_advancement,
    semantic_snapshot,
    validate_history,
)
from review_sensei_github.api_run_history import read_action_result
from review_sensei_github.api_run_requests import ApiRunRequestTransport
from review_sensei_github.http import GitHubHttp
from review_sensei_github.publication import parse_finding_index
from review_sensei_github.session_ledger import (
    GitHubIssueCommentSessionLedger,
    parse_session_comment,
)
from test_action_run import CommandEntryFixture
from test_api_work_persistence import HEAD, IDENTITY
from test_semantic_history_recovery_commands import _seed_two_status_streak

from review_sensei.disposition import MAINTAINER_ACTIONS
from review_sensei.errors import ReviewInputError
from review_sensei.finding_lifecycle_store import _validate_accepted_history_metadata
from review_sensei.schemas import validate_public_document


class SemanticHistoryRecoveryGuardsTests(unittest.TestCase):
    def test_target_allows_disposition_entry_only_for_state_advancing_commands(self):
        base, head = "a" * 40, "b" * 40
        for action in sorted(_STATE_ADVANCING_COMMAND_ACTIONS):
            caller = SimpleNamespace(
                _accepted_semantic_target=("command", base, head, action)
            )
            self.assertTrue(_target_allows_disposition_entry(caller), action)

        for target in (
            None,
            (),
            ("command", base, head),
            ("command", base, head, None),
            ("command", base, head, ""),
            ("command", base, head, "status"),
            ("command", base, head, "pause"),
            ("command", base, head, "verify"),
            ("command", base, head, "unknown"),
            ("reply", base, head, "dismiss"),
            ("reply", base, head, None),
            ("review", base, head, "dismiss"),
            ["command", base, head, "dismiss"],
        ):
            caller = SimpleNamespace(_accepted_semantic_target=target)
            self.assertFalse(_target_allows_disposition_entry(caller), repr(target))

    def test_state_advancing_and_non_advancing_action_sets_partition_maintainer_actions(
        self,
    ):
        self.assertEqual(
            _STATE_ADVANCING_COMMAND_ACTIONS | _NON_ADVANCING_COMMAND_ACTIONS,
            MAINTAINER_ACTIONS,
        )
        self.assertTrue(
            _STATE_ADVANCING_COMMAND_ACTIONS.isdisjoint(_NON_ADVANCING_COMMAND_ACTIONS)
        )

    def test_bootstrap_rejects_invalid_command_action_target(self):
        from argparse import Namespace

        for purpose, action in (
            ("review", "dismiss"),
            ("reply", "dismiss"),
            ("command", ""),
            ("command", " "),
            ("command", "unknown"),
            ("command", 123),
        ):
            with self.subTest(purpose=purpose, action=action):
                with self.assertRaisesRegex(ReviewInputError, "semantic target"):
                    review_run.bootstrap(
                        Namespace(diff=Path("unused.diff")),
                        run_directory=Path("unused"),
                        policy_commit="a" * 40,
                        purpose=purpose,
                        command_action=action,
                    )

    def test_run_action_classifies_all_command_routes_and_passes_command_action(self):
        fp, scope = "e" * 64, "a" * 64
        cases = (
            ("command", f'@reviewsensei dismiss {fp} --reason "safe"', "dismiss"),
            ("command", f'@reviewsensei defer {fp} --reason "later"', "defer"),
            (
                "command",
                f'@reviewsensei accept-risk {fp} --reason "mitigated"',
                "accept-risk",
            ),
            ("command", "@reviewsensei override RS-ABCDEF", "accept-risk"),
            (
                "command",
                "@reviewsensei override RS-ABCDEF acceptable risk",
                "accept-risk",
            ),
            ("command", "@reviewsensei RS-ABCDEF is acceptable risk", "accept-risk"),
            (
                "command",
                '@reviewsensei accept-coverage-uncertainty --reason "audited manually"',
                "accept-coverage-uncertainty",
            ),
            (
                "command",
                "@reviewsensei i reviewed the media files and i approve",
                "approve-media",
            ),
            ("command", "@reviewsensei review continue", "continue"),
            ("command", "@reviewsensei review reenroll", "reenroll"),
            (
                "command",
                f'@reviewsensei dismiss {fp} --reason "historical:{scope} Validated safe historical concern."',
                "dismiss",
            ),
            (
                "command",
                f'@reviewsensei accept-risk {fp} --reason "quarantine:{scope} Validated acceptable carried risk."',
                "accept-risk",
            ),
            ("command", "@reviewsensei review status", "status"),
            ("command", "@reviewsensei review pause", "pause"),
            ("command", "@reviewsensei verify", "verify"),
            ("reply", None, None),
        )
        for purpose, body, expected_action in cases:
            with (
                self.subTest(purpose=purpose, body=body),
                tempfile.TemporaryDirectory() as directory,
            ):
                captured = []
                sentinel = RuntimeError("stop-after-bootstrap")

                def fake_bootstrap(args, **kwargs):
                    captured.append((args, kwargs))
                    raise sentinel

                argv = [
                    "--policy-commit",
                    "a" * 40,
                    "--run-directory",
                    directory,
                    "--",
                    "--repository",
                    "owner/repo",
                    "--repository-id",
                    "123",
                    "--pull-request",
                    "7",
                    "--head-sha",
                    "b" * 40,
                    "--source-comment-id",
                    "41",
                    "--allow-write",
                ]
                if purpose == "command":
                    argv.extend(
                        [
                            "--comment-body",
                            body,
                            "--actor",
                            "alice",
                            "--association",
                            "OWNER",
                            "--github-session-ledger",
                        ]
                    )
                else:
                    argv.extend(
                        [
                            "--source-updated-at",
                            "2026-10-26T00:00:00Z",
                            "--provider",
                            "cloud-ollama",
                        ]
                    )

                with (
                    patch.object(
                        action_run,
                        "event_source",
                        return_value={"kind": "issue_comment", "id": 41},
                    ),
                    patch.object(action_run, "bootstrap", side_effect=fake_bootstrap),
                    self.assertRaises(RuntimeError) as caught,
                ):
                    action_run.run_action(argv, purpose=purpose)

                self.assertIs(caught.exception, sentinel)
                self.assertEqual(len(captured), 1)
                self.assertEqual(captured[0][1]["command_action"], expected_action)

        with tempfile.TemporaryDirectory() as directory:
            captured = []

            def fail_if_bootstrap(*args, **kwargs):
                captured.append((args, kwargs))
                raise AssertionError("bootstrap must not run on unrecognized command")

            argv = [
                "--policy-commit",
                "a" * 40,
                "--run-directory",
                directory,
                "--",
                "--repository",
                "owner/repo",
                "--repository-id",
                "123",
                "--pull-request",
                "7",
                "--head-sha",
                "b" * 40,
                "--source-comment-id",
                "41",
                "--allow-write",
                "--comment-body",
                "@reviewsensei unrecognized prose",
                "--actor",
                "alice",
                "--association",
                "OWNER",
                "--github-session-ledger",
            ]
            with (
                patch.object(action_run, "bootstrap", side_effect=fail_if_bootstrap),
                self.assertRaisesRegex(
                    ReviewInputError, "authenticated source is not a supported command"
                ),
            ):
                action_run.run_action(argv, purpose="command")
            self.assertEqual(captured, [])

    def test_execute_command_security_fence_rejects_mismatched_action_or_source(self):
        from review_sensei_github.api_run_host import review_startup_plan
        from review_sensei_github.review_run import ActionRunLedger, ReviewRun

        from review_sensei.api_work import (
            ApiExecutionOwner,
            ApiRunIdentity,
            FrozenApiConfiguration,
            initial_accounting,
        )
        from review_sensei.hosted import HostedPlan, HostedPolicy
        from tests.test_run_authority import HEAD as AUTH_HEAD
        from tests.test_run_authority import OPAQUE_GRANT, authority, granted

        cases = (
            (
                "body_mismatch",
                ("command", "a" * 40, AUTH_HEAD, "verify"),
                "@reviewsensei verify tampered",
                "authenticated source body conflicts with command",
                False,
            ),
            (
                "body_surrogate",
                ("command", "a" * 40, AUTH_HEAD, "verify"),
                "\ud800",
                "authenticated source body conflicts with command",
                False,
            ),
            (
                "missing_source_body_sha256",
                ("command", "a" * 40, AUTH_HEAD, "verify"),
                "@reviewsensei verify",
                "authenticated source body conflicts with command",
                True,
            ),
            (
                "action_mismatch",
                ("command", "a" * 40, AUTH_HEAD, "dismiss"),
                "@reviewsensei verify",
                "authenticated command action conflicts with admitted semantic target",
                False,
            ),
            (
                "reply_purpose_none_action",
                ("reply", "a" * 40, AUTH_HEAD, None),
                "@reviewsensei verify",
                "authenticated command action conflicts with admitted semantic target",
                False,
            ),
        )
        for fault, target, body, expected_error, drop_body_digest in cases:
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                grant_doc = granted(authority("command"))
                startup = review_startup_plan("command")
                owner = ApiExecutionOwner(run_attempt=1, job="command", generation=0)
                run_id = ApiRunIdentity(123, int(grant_doc["run_id"]))
                caller = ApiRunRequestTransport(
                    directory=Path(directory),
                    run=run_id,
                    plan=startup,
                    job="command",
                    attempt=1,
                    opener=lambda *a, **kw: None,
                )
                cfg = FrozenApiConfiguration("c" * 40, "d" * 64, 512)
                caller._state = initial_accounting(
                    run_id,
                    cfg,
                    owner,
                    startup,
                    prior_nonstartup_liability_upper_bound=0,
                ).reserve("hosted-review", 20)
                caller._block = "hosted-review"
                caller._accepted_semantic_target = target
                native_http = GitHubHttp(
                    api_url="https://api.github.test", opener=caller.open
                )
                native = GitHubIssueCommentSessionLedger(
                    native_http, token="synthetic", app_slug="reviewsensei[bot]"
                )
                run = ReviewRun(
                    caller=caller,
                    ledger=ActionRunLedger(native, caller, IDENTITY),
                    http=native_http,
                    broker=SimpleNamespace(
                        verify_session_grant=lambda tok, doc: grant_doc
                    ),
                    oidc="synthetic-oidc",
                    identity=IDENTITY,
                    plan=HostedPlan(
                        "synthetic",
                        "synthetic",
                        "https://provider.invalid",
                        False,
                        None,
                        False,
                        False,
                        "command",
                        "remote",
                        None,
                        30,
                        HostedPolicy(
                            False,
                            True,
                            "advisory",
                            True,
                            "disabled",
                            "disabled",
                            "enabled",
                            "default",
                        ),
                        None,
                        {},
                    ),
                    base_sha="a" * 40,
                    head_sha=AUTH_HEAD,
                    base_branch="main",
                    head_repository="owner/repo",
                    configuration_path=None,
                    session_authority=grant_doc,
                    session_grant=OPAQUE_GRANT,
                )
                args = SimpleNamespace(
                    comment_body=body,
                    source_comment_id=grant_doc["source"]["id"],
                    app_slug="reviewsensei[bot]",
                    review_mode=None,
                )
                with (
                    patch.object(
                        caller,
                        "admit_action",
                        side_effect=AssertionError("admit_action must not run"),
                    ),
                    self.assertRaisesRegex(ReviewInputError, expected_error),
                ):
                    if drop_body_digest:
                        malformed_doc = copy.deepcopy(grant_doc)
                        malformed_doc["source"].pop("body_sha256")
                        with patch.object(
                            action_run,
                            "VerifiedRunAction",
                            return_value=SimpleNamespace(
                                fence=lambda: None, document=malformed_doc
                            ),
                        ):
                            action_run.execute_command(run, args)
                    else:
                        action_run.execute_command(run, args)

    def test_disposition_entry_history_validation_compatibility(self):
        with tempfile.TemporaryDirectory() as directory:
            f, record, history = _seed_two_status_streak(directory)
            _, snapshot = capture_selected(f.selected.ledger, IDENTITY)
            validate_history(history, IDENTITY)
            binding = {"repository_id": 123, "pull_request": 7}
            _validate_accepted_history_metadata(history, binding)
            base_doc = copy.deepcopy(record.to_dict())
            base_doc["finding_lifecycle"]["accepted_semantic_history"] = history
            validate_public_document(base_doc, "session-record")

            source = {
                "kind": "issue_comment",
                "id": 99,
                "updated_at": "2026-11-13T00:00:00Z",
                "body_sha256": "a" * 64,
            }
            marked = append_history(
                history,
                IDENTITY,
                {"operation_key": "disp-1", "purpose": "command", "source": source},
                snapshot,
                snapshot,
                disposition_entry=True,
            )
            self.assertTrue(marked["records"][-1]["disposition_entry"])
            validate_history(marked, IDENTITY)
            _validate_accepted_history_metadata(marked, binding)
            for payload in (
                marked,
                {
                    "schema_version": "accepted-semantic-history-v1",
                    "qualification": "unavailable",
                    "reason": "test",
                    "prior": marked,
                },
            ):
                doc = copy.deepcopy(record.to_dict())
                doc["finding_lifecycle"]["accepted_semantic_history"] = payload
                validate_public_document(doc, "session-record")

            if snapshot["unresolved_count"] > 0:
                progressed_after = {
                    **snapshot,
                    "unresolved_count": snapshot["unresolved_count"] - 1,
                }
                progressed = append_history(
                    history,
                    IDENTITY,
                    {"operation_key": "disp-2", "purpose": "command", "source": source},
                    snapshot,
                    progressed_after,
                    disposition_entry=True,
                )
                self.assertTrue(progressed["records"][-1]["made_progress"])
                self.assertNotIn("disposition_entry", progressed["records"][-1])

            for label, mutator in (
                (
                    "false_flag",
                    lambda item: item.__setitem__("disposition_entry", False),
                ),
                (
                    "reply_purpose",
                    lambda item: item.update(
                        {"purpose": "reply", "disposition_entry": True}
                    ),
                ),
                (
                    "made_progress_true",
                    lambda item: item.update(
                        {"made_progress": True, "disposition_entry": True}
                    ),
                ),
            ):
                with self.subTest(label=label):
                    bad = copy.deepcopy(marked)
                    mutator(bad["records"][-1])
                    if label == "made_progress_true":
                        bad["streak"] = 0
                    with self.assertRaises(ReviewInputError):
                        validate_history(bad, IDENTITY)
                    with self.assertRaises(ReviewInputError):
                        _validate_accepted_history_metadata(bad, binding)
                    for payload in (
                        bad,
                        {
                            "schema_version": "accepted-semantic-history-v1",
                            "qualification": "unavailable",
                            "reason": "test",
                            "prior": bad,
                        },
                    ):
                        bad_doc = copy.deepcopy(record.to_dict())
                        bad_doc["finding_lifecycle"]["accepted_semantic_history"] = (
                            payload
                        )
                        with self.assertRaises(ReviewInputError):
                            validate_public_document(bad_doc, "session-record")

    def test_guard_carried_successor_rejects_forged_streak_and_foreign_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            f, record, history = _seed_two_status_streak(directory)
            caller = SimpleNamespace(
                _accepted_semantic_target=("command", "a" * 40, HEAD, "dismiss"),
                _root=record.record_sha256,
            )
            tampered_streak = copy.deepcopy(history)
            tampered_streak["streak"] = 0  # inconsistent with records[-2:]
            with self.assertRaisesRegex(ReviewInputError, "inconsistent_streak"):
                guard_carried_successor(
                    caller, f.selected.ledger, IDENTITY, record, tampered_streak
                )

            tampered_scope = copy.deepcopy(history)
            tampered_scope["repository_id"] = 999999
            with self.assertRaisesRegex(ReviewInputError, "foreign_scope"):
                guard_carried_successor(
                    caller, f.selected.ledger, IDENTITY, record, tampered_scope
                )

    def test_prepare_action_rejects_forged_predecessor(self):
        with tempfile.TemporaryDirectory() as directory:
            f, record, history = _seed_two_status_streak(directory)
            tampered_streak = copy.deepcopy(history)
            tampered_streak["streak"] = 0
            caller = SimpleNamespace(
                _accepted_semantic_target=("command", "a" * 40, HEAD, "dismiss"),
                _root=record.record_sha256,
                _accepted_semantic_predecessor=tampered_streak,
            )
            fake_run = SimpleNamespace(
                caller=caller,
                ledger=f.selected.ledger,
                identity=IDENTITY,
                base_sha="a" * 40,
            )
            with self.assertRaisesRegex(ReviewInputError, "inconsistent_streak"):
                prepare_action(fake_run, allow_unqualified=True)

    def test_dismiss_command_rejects_forged_retained_history(self):
        with tempfile.TemporaryDirectory() as directory:
            f, _, _ = _seed_two_status_streak(directory)
            index = parse_finding_index(
                f.reviews[-1]["body"],
                repository_id=123,
                pull_request=7,
                head_sha=HEAD,
            )
            assert index is not None
            instance = index.entries[0].instance
            saved = f.comment["body"]
            from review_sensei_github import api_run_history

            real_read = api_run_history.read_action_result
            reads = []

            def forged_read(ledger, identity, rec):
                reads.append(rec.record_sha256)
                retained = copy.deepcopy(real_read(ledger, identity, rec))
                retained["result"]["accepted_semantic_history"]["streak"] = 0
                return retained

            f.SOURCE_ID += 1
            f.select_command(
                f'@reviewsensei dismiss {instance} --reason "Should reject forged history."',
                new_run=True,
            )
            with patch.object(
                api_run_history, "read_action_result", side_effect=forged_read
            ):
                self.assertEqual(f.invoke_command(), 1)
            self.assertGreaterEqual(len(reads), 1)
            self.assertIn("accepted_semantic_history_inconsistent_streak", f.error)
            self.assertEqual(f.comment["body"], saved)

    def test_guard_carried_successor_still_rejects_corrupt_lifecycle_before_rollover(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            f = CommandEntryFixture(directory, limit=512)
            self.assertEqual(f.invoke(human=True), 0, f.error)
            f.SOURCE_ID = 41
            f.select_command("@reviewsensei review status", new_run=True)
            self.assertEqual(f.invoke_command(), 0, f.error)
            prior_record = parse_session_comment(f.comment["body"], identity=IDENTITY)
            f.SOURCE_ID = 42
            f.select_command("@reviewsensei review status", new_run=True)
            self.assertEqual(f.invoke_command(), 0, f.error)
            record = parse_session_comment(f.comment["body"], identity=IDENTITY)
            history = read_action_result(f.selected.ledger, IDENTITY, record)["result"][
                "accepted_semantic_history"
            ]
            self.assertEqual(history["streak"], LIMIT)

            caller = SimpleNamespace(
                _accepted_semantic_target=("command", "a" * 40, HEAD, "dismiss"),
                _root=prior_record.record_sha256,
            )
            with self.assertRaisesRegex(
                ReviewInputError, "accepted_semantic_history_selected_root_changed"
            ):
                guard_carried_successor(
                    caller, f.selected.ledger, IDENTITY, prior_record, history
                )

            with (
                patch(
                    "review_sensei.finding_lifecycle_store.select_finding_lifecycle",
                    return_value=(SimpleNamespace(status="ok", record=record), None),
                ),
                self.assertRaisesRegex(
                    ReviewInputError,
                    "accepted_semantic_history_complete_inventory_required",
                ),
            ):
                guard_carried_successor(
                    caller, f.selected.ledger, IDENTITY, record, history
                )

    def test_semantic_snapshot_observes_partial_coverage_uncertainty_acceptance(self):
        from review_sensei.active_finding_window import (
            apply_dispositions_to_window_state,
            evaluate_active_window_round,
        )
        from review_sensei.coverage import CoverageManifest, FileCoverage
        from review_sensei.finding_drift import (
            apply_dispositions_to_drift_state,
            evaluate_drift_round,
        )
        from review_sensei.models import ReviewResult

        cov = CoverageManifest(
            files=(FileCoverage("img.bin", "unsupported", "binary"),),
            enumeration_complete=True,
            enumerated_paths=("img.bin",),
        )
        res = ReviewResult(
            provider="internal",
            summary="partial",
            comments=(),
            review_status="partial",
            coverage=cov,
        )
        journal = {"effects": [], "unknown_trust_effect": False, "native_receipts": []}

        w0 = evaluate_active_window_round(
            res, head_sha=HEAD, base_sha="a" * 40, max_active_findings=5
        )
        w1 = apply_dispositions_to_window_state(
            w0,
            honored_fingerprints=(),
            honored_instances=(),
            actor="alice",
            head_sha=HEAD,
            slot_released=True,
            coverage_uncertainty_accepted=True,
        )
        sw0, sw1 = semantic_snapshot(w0, journal), semantic_snapshot(w1, journal)
        self.assertEqual(sw0["unresolved_count"] - 1, sw1["unresolved_count"])
        self.assertEqual(sw0["inventory_sha256"], sw1["inventory_sha256"])
        self.assertTrue(semantic_advancement(sw0, sw1))
        self.assertFalse(semantic_advancement(sw1, sw1))

        d0 = evaluate_drift_round(
            res, head_sha=HEAD, base_sha="a" * 40, max_active_findings=5
        )
        d1 = apply_dispositions_to_drift_state(
            d0,
            honored_fingerprints=(),
            honored_instances=(),
            actor="alice",
            head_sha=HEAD,
            slot_released=True,
            coverage_uncertainty_accepted=True,
        )
        sd0, sd1 = semantic_snapshot(d0, journal), semantic_snapshot(d1, journal)
        self.assertEqual(sd0["unresolved_count"] - 1, sd1["unresolved_count"])
        self.assertEqual(sd0["inventory_sha256"], sd1["inventory_sha256"])
        self.assertTrue(semantic_advancement(sd0, sd1))
        self.assertFalse(semantic_advancement(sd1, sd1))

    def test_historical_snapshot_observes_historical_assessment_settlement(self):
        from test_action_run import historical_body, historical_command_case

        from review_sensei.active_finding_window import evaluate_active_window_round
        from review_sensei.models import ReviewResult

        with tempfile.TemporaryDirectory() as directory:
            f = historical_command_case(directory)
            base_snap = semantic_snapshot(
                evaluate_active_window_round(
                    ReviewResult(
                        provider="internal",
                        summary="ok",
                        comments=(),
                        review_status="complete",
                    ),
                    head_sha=f.requested_head,
                    base_sha="a" * 40,
                    max_active_findings=5,
                ),
                {"effects": [], "unknown_trust_effect": False, "native_receipts": []},
            )
            before_rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
            before_snap = historical_snapshot(base_snap, f.selected.ledger, before_rec)
            hold = before_rec.historical_assessments
            self.assertIsNotNone(hold)
            f.select_command(historical_body(hold, action="dismiss"), new_run=True)
            self.assertEqual(f.invoke_command(), 0, (f.error, f.stdout))
            after_rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
            after_snap = historical_snapshot(base_snap, f.selected.ledger, after_rec)
            self.assertEqual(
                before_snap["unresolved_count"] - 1, after_snap["unresolved_count"]
            )
            self.assertEqual(
                before_snap["inventory_sha256"], after_snap["inventory_sha256"]
            )
            self.assertNotEqual(
                before_snap["unresolved_sha256"], after_snap["unresolved_sha256"]
            )
            self.assertTrue(semantic_advancement(before_snap, after_snap))
            self.assertFalse(semantic_advancement(after_snap, after_snap))
            retained = append_history(
                None,
                IDENTITY,
                {
                    "operation_key": "historical-dismiss-1",
                    "purpose": "command",
                    "source": {
                        "kind": "issue_comment",
                        "id": f.SOURCE_ID,
                        "updated_at": f.SOURCE_STAMP,
                        "body_sha256": "a" * 64,
                    },
                },
                before_snap,
                after_snap,
                disposition_entry=True,
            )
            self.assertEqual(retained["streak"], 0)
            self.assertTrue(retained["records"][-1]["made_progress"])
            self.assertNotIn("disposition_entry", retained["records"][-1])


if __name__ == "__main__":
    unittest.main()
