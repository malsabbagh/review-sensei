"""Historical commands retain exact syntax and current native publication proof."""

import unittest
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from review_sensei_github import action_run
from review_sensei_github.approval import ApprovalFacts, ReviewApprovalEligibility
from review_sensei_github.historical_assessment_action import (
    HistoricalTargetRefusal,
    parse_historical_command,
    prepare_historical_target,
)
from review_sensei_github.native_union import NativeUnionReceipt
from review_sensei_github.publication import (
    approval_eligibility_marker,
    finding_index_marker,
)
from test_native_review_census import NativeCensusBackend

from review_sensei.errors import ReviewInputError
from review_sensei.historical_assessments import digest

SCOPE = "a" * 64
INSTANCE = "e" * 64
REASON = "The original evidence disproves this historical concern."


def command_body(action="dismiss", reference=INSTANCE, namespace="historical:"):
    return f'@reviewsensei {action} {reference} --reason "{namespace}{SCOPE} {REASON}"'


class HistoricalCommandCompositionTests(unittest.TestCase):
    def test_complete_historical_command_keeps_exact_target_and_submitted_reason(self):
        for action in ("dismiss", "accept-risk"):
            with self.subTest(action=action):
                command = parse_historical_command(command_body(action))
                self.assertEqual(command.action, action)
                self.assertEqual(command.scope, SCOPE)
                self.assertEqual(command.reference, "RS-" + INSTANCE.upper())
                self.assertEqual(command.reason, REASON)
                self.assertEqual(
                    command.submitted_reason, f"historical:{SCOPE} {REASON}"
                )

    def test_malformed_historical_namespace_never_falls_through(self):
        for body in (
            command_body(reference="abc123"),
            command_body(reference="g" * 64),
            command_body(reference="e" * 65),
            command_body(reference="RS-" + INSTANCE),
            command_body(action="resolve"),
            command_body(action="defer"),
            command_body(namespace="historical/"),
            command_body(namespace="HISTORICAL:", reference="bad"),
            command_body(reference="bad").replace('"', "'"),
            command_body().replace("dismiss", "review dismiss"),
        ):
            with self.subTest(body=body), self.assertRaises(ReviewInputError):
                parse_historical_command(body)

    def test_unrelated_commands_are_not_historical_targets(self):
        for body in (
            None,
            "ordinary prose",
            "@reviewsensei review status",
            command_body().replace(f"historical:{SCOPE} ", ""),
            command_body(namespace="quarantine:"),
        ):
            with self.subTest(body=body):
                self.assertIsNone(parse_historical_command(body))

    def test_malformed_reserved_commands_refuse_before_event_or_bootstrap(self):
        for namespace in ("historical:", "quarantine:"):
            for reference in ("bad", "g" * 64):
                with (
                    self.subTest(namespace=namespace, reference=reference),
                    TemporaryDirectory() as directory,
                ):
                    arguments = [
                        "--policy-commit",
                        "a" * 40,
                        "--run-directory",
                        directory,
                        "--",
                        "--repository",
                        "owner/repo",
                        "--repository-id",
                        "1",
                        "--pull-request",
                        "1",
                        "--head-sha",
                        "b" * 40,
                        "--source-comment-id",
                        "41",
                        "--comment-body",
                        command_body(reference=reference, namespace=namespace),
                        "--actor",
                        "untrusted",
                        "--association",
                        "NONE",
                        "--github-session-ledger",
                        "--allow-write",
                    ]
                    with (
                        patch.object(
                            action_run,
                            "event_source",
                            side_effect=AssertionError("event read"),
                        ) as event,
                        patch.object(
                            action_run,
                            "bootstrap",
                            side_effect=AssertionError("bootstrap"),
                        ) as bootstrap,
                        self.assertRaisesRegex(
                            ReviewInputError, "complete supported finding syntax"
                        ),
                    ):
                        action_run.run_action(arguments, purpose="command")
                    event.assert_not_called()
                    bootstrap.assert_not_called()


def historical_native_target():
    backend = NativeCensusBackend(items=(), old_count=0)
    plan = backend.plan
    receipt = NativeUnionReceipt(
        repository_id=plan.repository_id,
        pull_request=plan.pull_request,
        producer_id=999,
        base_sha=plan.base_sha,
        head_sha=plan.head_sha,
        transaction_id=plan.transaction_id,
        result_sha256=plan.result_sha256,
        plan_sha256=plan.sha256,
        inline_count=0,
        parts=(),
    )
    hold = {
        "scope_sha256": SCOPE,
        "assessments": [{"instance": INSTANCE, "concern": "f" * 64}],
        "settlements": [],
    }
    eligibility = ReviewApprovalEligibility(
        head_sha=plan.head_sha,
        result_digest=receipt.result_digest,
        facts=ApprovalFacts(
            enabled=True,
            app_authored=False,
            review_status="complete",
            evidence_policy="legacy",
            has_blocking_findings=False,
            has_human_adjudication_findings=True,
            coverage_blocker=None,
        ),
        historical_assessments={
            "scope_sha256": SCOPE,
            "state_sha256": digest(hold),
            "pending": [INSTANCE],
            "fresh_human_pending": False,
            "review_id": 41,
            "historical_head_sha": "a" * 40,
        },
        native_union=receipt,
    )
    backend.main_body += "\n\n" + finding_index_marker(
        repository_id=plan.repository_id,
        pull_request=plan.pull_request,
        head_sha=plan.head_sha,
        base_sha=plan.base_sha,
        result_digest=receipt.result_digest,
        entries=(),
    )
    backend.main_body += "\n\n" + approval_eligibility_marker(eligibility)
    selected = {
        "id": backend.main_id,
        "commit_id": plan.head_sha,
        "user": {"id": 999, "type": "Bot"},
        "body": backend.main_body,
    }
    run = Mock(head_sha=plan.head_sha)
    run.identity = SimpleNamespace(
        repository="owner/repo", repository_id=1, pull_request=1
    )
    run.ledger._evidence_author_id = 999
    run.ledger.load.return_value.record.transaction = plan
    run.http.paginate.return_value = [selected]
    authority = Mock(document={"actor": {"association": "MEMBER"}})
    app = Mock()
    historical_reader = app.reviewer.finalizer.historical_reader.return_value
    historical_reader.read.return_value = hold
    human_reader = app._human_inventory_reader.return_value
    human_reader.native_union_reader.return_value = lambda union: backend.read()
    return run, authority, app, selected, hold, eligibility


class HistoricalTargetCompositionTests(unittest.TestCase):
    def test_historical_target_uses_selected_reviews_native_proof_from_same_reader(
        self,
    ):
        run, authority, app, selected, hold, eligibility = historical_native_target()
        target = prepare_historical_target(
            run, authority, parse_historical_command(command_body()), app, "maintain"
        )
        human_reader = app._human_inventory_reader.return_value
        app._human_inventory_reader.assert_called_once()
        human_reader.native_union_reader.assert_called_once_with(selected)
        historical_reader = app.reviewer.finalizer.historical_reader.return_value
        historical_reader.require.assert_called_once_with(
            eligibility, head_sha=run.head_sha
        )
        self.assertEqual(target.hold, hold)
        self.assertEqual(target.instance, INSTANCE)
        authority.fence.assert_called_once_with()
        run.ledger.replace.assert_not_called()

    def test_historical_target_rejects_missing_native_proof_before_authority_fence(
        self,
    ):
        run, authority, app, _, _, _ = historical_native_target()
        app._human_inventory_reader.return_value.native_union_reader.return_value = None
        with self.assertRaisesRegex(
            ReviewInputError, "no explicit historical assessment marker"
        ):
            prepare_historical_target(
                run,
                authority,
                parse_historical_command(command_body()),
                app,
                "maintain",
            )
        authority.fence.assert_not_called()
        run.ledger.replace.assert_not_called()

    def test_historical_target_still_requires_current_maintainer_permission(self):
        run, authority, app, _, _, _ = historical_native_target()
        with self.assertRaisesRegex(
            HistoricalTargetRefusal, "current maintain or admin"
        ):
            prepare_historical_target(
                run, authority, parse_historical_command(command_body()), app, "write"
            )
        app.reviewer.finalizer.historical_reader.assert_not_called()
        authority.fence.assert_not_called()
        run.ledger.replace.assert_not_called()
