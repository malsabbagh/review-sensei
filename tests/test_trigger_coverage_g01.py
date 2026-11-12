"""G01: Automatic trigger coverage, caller parity, and parser -> admission tests."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from review_sensei_github.event_routing_contract import (
    CALLER_SUBSCRIBED_EVENTS,
    EVENT_DECISION_TABLE,
    INSTALLED_CALLER_SUBSCRIPTION_ROUTES,
    MAX_EVENT_PAYLOAD_BYTES,
    REQUIRED_EVENT_FAMILIES,
    RetainedSnapshotState,
    decision_rule_for,
    evaluate_github_event,
    inspect_caller_subscription_contract,
    map_installed_caller_subscription,
)
from review_sensei_github.setup import (
    SetupPlanBuilder,
    migrate_caller_preserving_customizations,
)
from review_sensei_github.webhooks import WebhookVerifier

from review_sensei.hosting.github import trigger as hosting_trigger

ROOT = Path(__file__).resolve().parents[1]
BASE_SHA_1 = "1" * 40
BASE_SHA_2 = "2" * 40
HEAD_SHA_1 = "a" * 40
HEAD_SHA_2 = "b" * 40


def _pull_payload(
    *,
    number: int = 42,
    head_sha: str = HEAD_SHA_1,
    head_ref: str = "feat/event-routing",
    base_sha: str = BASE_SHA_1,
    base_ref: str = "main",
    draft: object = False,
    state: str = "open",
    include_draft: bool = True,
) -> dict[str, Any]:
    pull: dict[str, Any] = {
        "number": number,
        "state": state,
        "title": "Harden event routing",
        "head": {"sha": head_sha, "ref": head_ref},
        "base": {"sha": base_sha, "ref": base_ref},
    }
    if include_draft:
        pull["draft"] = draft
    return pull


class TriggerCoverageG01Tests(unittest.TestCase):
    def test_checked_in_caller_example_and_setup_templates_match_approved_boundary(
        self,
    ) -> None:
        checked_in = (ROOT / ".github/workflows/review-sensei-review.yml").read_text(
            encoding="utf-8"
        )
        example = (ROOT / "examples/github-actions/review-sensei-review.yml").read_text(
            encoding="utf-8"
        )
        setup_plan = SetupPlanBuilder().build(
            "owner/repo",
            base_branch="main",
            base_sha=BASE_SHA_1,
        )
        generated = {item.path: item.content for item in setup_plan.files}[
            ".github/workflows/review-sensei-review.yml"
        ]

        for label, content in (
            ("checked_in", checked_in),
            ("example", example),
            ("setup_generated", generated),
        ):
            with self.subTest(caller=label):
                report = inspect_caller_subscription_contract(content)
                self.assertTrue(report.matches_approved_boundary)
                self.assertEqual(
                    report.pull_request_types,
                    CALLER_SUBSCRIBED_EVENTS["pull_request"],
                )
                self.assertEqual(
                    report.issue_comment_types,
                    CALLER_SUBSCRIBED_EVENTS["issue_comment"],
                )
                self.assertEqual(
                    report.review_comment_types,
                    CALLER_SUBSCRIBED_EVENTS["pull_request_review_comment"],
                )
                self.assertTrue(report.has_workflow_dispatch)
                self.assertEqual(report.unapproved_triggers, ())

        fixtures_dir = (
            ROOT
            / "packages/python/review-sensei-github/src/review_sensei_github/fixtures"
        )
        for fixture_name in (
            "merge-focused-v4-caller.yml",
            "sensei-only-v5-caller.yml",
            "released-v5-keyword-rescan-caller.yml",
            "released-v5-pre-recovery-caller.yml",
        ):
            with self.subTest(migrated_fixture=fixture_name):
                raw_fixture = (fixtures_dir / fixture_name).read_text(encoding="utf-8")
                migrated, _diff = migrate_caller_preserving_customizations(raw_fixture)
                migrated_report = inspect_caller_subscription_contract(migrated)
                self.assertTrue(migrated_report.matches_approved_boundary)

    def test_caller_inspector_supports_block_lists_and_flags_unapproved_drift(
        self,
    ) -> None:
        with self.assertRaises(ValueError):
            inspect_caller_subscription_contract("   ")

        checked_in = (ROOT / ".github/workflows/review-sensei-review.yml").read_text(
            encoding="utf-8"
        )
        block_list_variant = checked_in.replace(
            "  pull_request:\n    types: [opened, reopened, synchronize, ready_for_review]",
            "  pull_request:\n    types:\n      - opened\n      - reopened\n      - synchronize\n      - ready_for_review",
        )
        self.assertTrue(
            inspect_caller_subscription_contract(
                block_list_variant
            ).matches_approved_boundary
        )

        with_merge_group = checked_in.replace(
            "  pull_request:\n",
            "  merge_group: [checks_requested]\n  pull_request:\n",
        )
        mg_report = inspect_caller_subscription_contract(with_merge_group)
        self.assertFalse(mg_report.matches_approved_boundary)
        self.assertIn("merge_group", mg_report.unapproved_triggers)

        with_edited = checked_in.replace(
            "types: [opened, reopened, synchronize, ready_for_review]",
            "types: [opened, reopened, synchronize, ready_for_review, edited]",
        )
        edited_report = inspect_caller_subscription_contract(with_edited)
        self.assertFalse(edited_report.matches_approved_boundary)
        self.assertIn("pull_request:edited", edited_report.unapproved_triggers)

        wildcard_pr = checked_in.replace(
            "    types: [opened, reopened, synchronize, ready_for_review]\n",
            "",
        )
        wildcard_report = inspect_caller_subscription_contract(wildcard_pr)
        self.assertFalse(wildcard_report.matches_approved_boundary)
        self.assertIn("pull_request:*", wildcard_report.unapproved_triggers)

        write_escalated = checked_in.replace(
            "  pull-requests: read",
            "  pull-requests: write",
            1,
        )
        self.assertFalse(
            inspect_caller_subscription_contract(
                write_escalated
            ).read_only_caller_permissions
        )

        quoted_keys = (
            checked_in.replace("\non:\n", '\n"on":\n', 1)
            .replace("\npermissions:\n", '\n"permissions":\n', 1)
            .replace("    types: [", '    "types": [', 1)
        )
        self.assertTrue(
            inspect_caller_subscription_contract(quoted_keys).matches_approved_boundary
        )

        comment_spoofed_guard = checked_in.replace(
            "github.event.comment.user.type != 'Bot'",
            "true # github.event.comment.user.type != 'Bot'",
        )
        spoofed_report = inspect_caller_subscription_contract(comment_spoofed_guard)
        self.assertFalse(spoofed_report.enforces_non_bot_comments)
        self.assertFalse(spoofed_report.matches_approved_boundary)

    def test_decision_table_covers_all_required_event_families(self) -> None:
        covered_families = {rule.family for rule in EVENT_DECISION_TABLE}
        self.assertEqual(covered_families, set(REQUIRED_EVENT_FAMILIES))
        for rule in EVENT_DECISION_TABLE:
            with self.subTest(rule_id=rule.rule_id):
                self.assertEqual(decision_rule_for(rule.rule_id), rule)
                self.assertTrue(rule.preserves_prior_obligations)
                self.assertTrue(rule.read_time_guard)
                if rule.rule_id == "merge_group.queue":
                    self.assertIsNone(rule.recovery_command)
                    self.assertFalse(rule.caller_subscribed)
                    self.assertFalse(rule.allows_model_inference)
                else:
                    self.assertIsNotNone(rule.recovery_command)
                if not rule.caller_subscribed:
                    self.assertFalse(rule.allows_model_inference)

        with self.assertRaises(KeyError):
            decision_rule_for("nonexistent.rule")

    def test_all_pull_request_actions_and_draft_controls_route_deterministically(
        self,
    ) -> None:
        for action in ("opened", "reopened", "synchronize", "ready_for_review"):
            with self.subTest(subscribed_action=action):
                raw_bytes = json.dumps(
                    {
                        "action": action,
                        "pull_request": _pull_payload(),
                        "sender": {"login": "alice", "type": "User"},
                    }
                ).encode("utf-8")
                decision = evaluate_github_event(
                    "pull_request",
                    raw_bytes,
                    delivery_id=f"deliv-{action}",
                )
                self.assertTrue(decision.caller_subscribed)
                self.assertTrue(decision.admitted)
                self.assertEqual(decision.disposition, "admitted_review")
                self.assertEqual(decision.outcome_code, "review_scheduled")
                self.assertEqual(decision.operation, "review")
                self.assertTrue(decision.requires_model_inference)
                self.assertIsNotNone(decision.resolution)
                assert decision.resolution is not None
                self.assertEqual(decision.resolution.head_sha, HEAD_SHA_1)
                self.assertEqual(
                    decision.source_identity.source_dedup_key,
                    f"delivery:deliv-{action}",
                )

        draft_true = evaluate_github_event(
            "pull_request",
            {"action": "opened", "pull_request": _pull_payload(draft=True)},
        )
        self.assertFalse(draft_true.admitted)
        self.assertEqual(draft_true.disposition, "explicit_noop")
        self.assertEqual(draft_true.outcome_code, "draft_skipped")
        self.assertFalse(draft_true.requires_model_inference)

        for label, pull in (
            ("absent", _pull_payload(include_draft=False)),
            ("null", _pull_payload(draft=None)),
            ("zero", _pull_payload(draft=0)),
            ("one", _pull_payload(draft=1)),
            ("str_false", _pull_payload(draft="false")),
            ("str_true", _pull_payload(draft="true")),
        ):
            with self.subTest(malformed_draft=label):
                bad_draft = evaluate_github_event(
                    "pull_request",
                    {"action": "synchronize", "pull_request": pull},
                )
                self.assertFalse(bad_draft.admitted)
                self.assertEqual(bad_draft.disposition, "rejected_guard")
                self.assertEqual(bad_draft.outcome_code, "draft_boolean_invalid")
                self.assertFalse(bad_draft.requires_model_inference)

        closed_sync = evaluate_github_event(
            "pull_request",
            {
                "action": "synchronize",
                "pull_request": _pull_payload(state="closed"),
            },
        )
        self.assertFalse(closed_sync.admitted)
        self.assertEqual(closed_sync.outcome_code, "closed_pr_rejected_at_admission")

        for action, expected_code in (
            ("edited", "title_or_metadata_edited_noop"),
            ("converted_to_draft", "converted_to_draft_noop"),
            ("closed", "closed_pr_noop"),
            ("labeled", "unsubscribed_pull_request_action"),
        ):
            with self.subTest(unsubscribed_action=action):
                decision = evaluate_github_event(
                    "pull_request",
                    {
                        "action": action,
                        "changes": {"title": {"from": "Old title"}},
                        "pull_request": _pull_payload(),
                    },
                )
                self.assertFalse(decision.caller_subscribed)
                self.assertFalse(decision.admitted)
                self.assertEqual(decision.outcome_code, expected_code)
                self.assertFalse(decision.requires_model_inference)

    def test_non_pr_auxiliary_events_and_malformed_envelopes_fail_closed_gracefully(
        self,
    ) -> None:
        retained = RetainedSnapshotState(
            reviewed_base_sha=BASE_SHA_1,
            reviewed_base_ref="main",
            reviewed_head_sha=HEAD_SHA_1,
            open_finding_ids=("rs-f001", "rs-f002"),
        )
        cases = (
            (
                "create",
                {"ref": "feat/event-routing", "ref_type": "branch"},
                "branch_recreation_invalidation",
            ),
            (
                "delete",
                {"ref": "feat/event-routing", "ref_type": "branch"},
                "branch_recreation_invalidation",
            ),
            (
                "pull_request_review_thread",
                {
                    "action": "resolved",
                    "pull_request": {"number": 42},
                    "sender": {"login": "alice", "type": "User"},
                },
                "thread_event_manual_verify_only",
            ),
            (
                "pull_request_review",
                {
                    "action": "submitted",
                    "pull_request": {"number": 42},
                    "review": {
                        "state": "approved",
                        "author_association": "MEMBER",
                        "user": {"login": "bob", "type": "User"},
                    },
                },
                "human_review_independent_noop",
            ),
            (
                "merge_group",
                {"action": "checks_requested"},
                "merge_group_unsupported",
            ),
            (
                "deployment_status",
                {"action": "created"},
                "unsupported_event",
            ),
        )
        for event_name, payload, expected_code in cases:
            with self.subTest(event_name=event_name):
                decision = evaluate_github_event(
                    event_name,
                    payload,
                    retained_state=retained,
                )
                self.assertFalse(decision.caller_subscribed)
                self.assertFalse(decision.admitted)
                self.assertEqual(decision.outcome_code, expected_code)
                self.assertFalse(decision.requires_model_inference)
                self.assertEqual(
                    decision.preserved_open_finding_ids,
                    ("rs-f001", "rs-f002"),
                )

        for bad_event, bad_payload in (
            ("", {"action": "opened"}),
            ("pull_request", b"\xff\xfe"),
            ("pull_request", "{not-json"),
            ("pull_request", "[]"),
            ("pull_request", 12345),
            ("pull_request", b'{"a":"' + (b"x" * MAX_EVENT_PAYLOAD_BYTES) + b'"}'),
            ("pull_request", '{"a":"' + ("x" * MAX_EVENT_PAYLOAD_BYTES) + '"}'),
        ):
            with self.subTest(bad_event=bad_event, bad_payload=repr(bad_payload)):
                decision = evaluate_github_event(
                    bad_event,
                    bad_payload,
                    retained_state=retained,
                )
                self.assertFalse(decision.admitted)
                self.assertEqual(decision.disposition, "malformed_payload")
                self.assertEqual(decision.outcome_code, "malformed_event_payload")
                self.assertEqual(
                    decision.preserved_open_finding_ids,
                    ("rs-f001", "rs-f002"),
                )

        bad_pr = evaluate_github_event(
            "pull_request",
            {"action": "opened", "pull_request": "not-an-object"},
        )
        self.assertEqual(bad_pr.outcome_code, "malformed_pull_request_payload")

        bad_sha = evaluate_github_event(
            "pull_request",
            {
                "action": "opened",
                "pull_request": _pull_payload(head_sha="not-a-sha"),
            },
        )
        self.assertEqual(bad_sha.outcome_code, "invalid_pull_request_identity")

    def test_comment_events_preserve_command_precedence_and_actor_guards(self) -> None:
        live_pr = _pull_payload()

        def comment_event(
            body: object,
            *,
            action: str = "created",
            actor_type: str | None = "User",
            association: str = "MEMBER",
            is_pr: bool = True,
            comment_id: int = 9001,
        ) -> dict[str, Any]:
            issue_obj: dict[str, Any] = {"number": 42}
            if is_pr:
                issue_obj["pull_request"] = {
                    "url": "https://api.github.com/repos/o/r/pulls/42"
                }
            user_obj: dict[str, Any] = {"login": "maintainer"}
            if actor_type is not None:
                user_obj["type"] = actor_type
            return {
                "action": action,
                "issue": issue_obj,
                "comment": {
                    "id": comment_id,
                    "body": body,
                    "updated_at": "2026-11-11T00:00:00Z",
                    "author_association": association,
                    "user": user_obj,
                },
            }

        rescan = evaluate_github_event(
            "issue_comment",
            comment_event(f"@reviewsensei rescan commit {HEAD_SHA_1[:10]}"),
            live_pull_request=live_pr,
        )
        self.assertTrue(rescan.admitted)
        self.assertEqual(rescan.operation, "review")
        self.assertEqual(rescan.command_precedence_tier, "rescan_or_remaining_review")
        self.assertEqual(rescan.outcome_code, "rescan_review_scheduled")
        self.assertTrue(rescan.requires_model_inference)

        remaining = evaluate_github_event(
            "issue_comment",
            comment_event("@reviewsensei review remaining"),
            live_pull_request=live_pr,
        )
        self.assertTrue(remaining.admitted)
        self.assertEqual(remaining.operation, "review")
        self.assertEqual(
            remaining.command_precedence_tier, "rescan_or_remaining_review"
        )
        self.assertEqual(remaining.outcome_code, "remaining_review_scheduled")

        verify_cmd = evaluate_github_event(
            "issue_comment",
            comment_event("@reviewsensei verify"),
            live_pull_request=live_pr,
        )
        self.assertTrue(verify_cmd.admitted)
        self.assertEqual(verify_cmd.operation, "command")
        self.assertEqual(verify_cmd.command_precedence_tier, "maintainer_command")
        self.assertEqual(verify_cmd.outcome_code, "command_scheduled")
        self.assertFalse(verify_cmd.requires_model_inference)

        reply = evaluate_github_event(
            "issue_comment",
            comment_event("@reviewsensei can you explain why this edge case matters?"),
            live_pull_request=live_pr,
        )
        self.assertTrue(reply.admitted)
        self.assertEqual(reply.operation, "reply")
        self.assertEqual(reply.command_precedence_tier, "mention_reply")
        self.assertEqual(reply.outcome_code, "reply_scheduled")
        self.assertTrue(reply.requires_model_inference)

        inline_reply = evaluate_github_event(
            "pull_request_review_comment",
            {
                "action": "created",
                "comment": {
                    "id": 9002,
                    "body": "@sensei please clarify this check",
                    "updated_at": "2026-11-11T00:01:00Z",
                    "pull_request_url": "https://api.github.com/repos/o/r/pulls/42",
                    "author_association": "COLLABORATOR",
                    "user": {"login": "collab", "type": "User"},
                },
            },
            live_pull_request=live_pr,
        )
        self.assertTrue(inline_reply.admitted)
        self.assertEqual(inline_reply.operation, "reply")
        self.assertEqual(inline_reply.source_identity.pull_request_number, 42)
        self.assertEqual(inline_reply.source_identity.source_kind, "inline")

        for mutated_action in ("edited", "deleted"):
            mutated = evaluate_github_event(
                "issue_comment",
                comment_event("@reviewsensei verify", action=mutated_action),
                live_pull_request=live_pr,
            )
            self.assertFalse(mutated.caller_subscribed)
            self.assertFalse(mutated.admitted)
            self.assertEqual(mutated.outcome_code, "comment_mutation_invalidated")

        unsub_comment = evaluate_github_event(
            "issue_comment",
            comment_event("@reviewsensei verify", action="pinned"),
            live_pull_request=live_pr,
        )
        self.assertEqual(unsub_comment.outcome_code, "unsubscribed_comment_action")

        self.assertEqual(
            evaluate_github_event(
                "issue_comment",
                comment_event(None),
                live_pull_request=live_pr,
            ).outcome_code,
            "malformed_comment_payload",
        )
        self.assertEqual(
            evaluate_github_event(
                "issue_comment",
                comment_event("@reviewsensei verify", is_pr=False),
                live_pull_request=live_pr,
            ).outcome_code,
            "not_pull_request_comment",
        )
        self.assertEqual(
            evaluate_github_event(
                "issue_comment",
                comment_event("@reviewsensei verify", actor_type="Bot"),
                live_pull_request=live_pr,
            ).outcome_code,
            "bot_actor_ignored",
        )
        self.assertEqual(
            evaluate_github_event(
                "issue_comment",
                comment_event("@reviewsensei verify", actor_type=None),
                live_pull_request=live_pr,
            ).outcome_code,
            "actor_type_missing",
        )
        self.assertEqual(
            evaluate_github_event(
                "issue_comment",
                comment_event("@reviewsensei verify", association="CONTRIBUTOR"),
                live_pull_request=live_pr,
            ).outcome_code,
            "association_unauthorized",
        )
        self.assertEqual(
            evaluate_github_event(
                "issue_comment",
                comment_event("looks good to me"),
                live_pull_request=live_pr,
            ).outcome_code,
            "no_sensei_mention",
        )
        self.assertEqual(
            evaluate_github_event(
                "issue_comment",
                comment_event(f"@reviewsensei rescan commit {HEAD_SHA_2}"),
                live_pull_request=live_pr,
            ).outcome_code,
            "requested_commit_mismatch",
        )
        self.assertEqual(
            evaluate_github_event(
                "issue_comment",
                comment_event("@reviewsensei verify"),
                live_pull_request=None,
            ).outcome_code,
            "live_pull_request_unavailable",
        )

        # Non-PR issue comments exclude issue numbers and reject edited/deleted consistently.
        for non_pr_action in ("created", "edited", "deleted"):
            non_pr_decision = evaluate_github_event(
                "issue_comment",
                comment_event(
                    "@reviewsensei verify", action=non_pr_action, is_pr=False
                ),
                live_pull_request=live_pr,
            )
            self.assertFalse(non_pr_decision.admitted)
            self.assertEqual(non_pr_decision.outcome_code, "not_pull_request_comment")
            self.assertIsNone(non_pr_decision.source_identity.pull_request_number)

        # Handle boundaries, homoglyphs, inline code spans, and closed/unclosed fences do not admit.
        for non_actionable_body in (
            "@reviewsensei_bot please review",
            "@sensei_bot please review",
            "contact dev@sensei.example for help",
            "@r\u0435viewsensei rescan",
            "Use `@reviewsensei verify` after fixing",
            "Example:\n```\n@reviewsensei rescan\n```\n",
            "Unclosed fence:\n```yaml\n@sensei verify\n",
        ):
            with self.subTest(non_actionable_body=non_actionable_body):
                ignored = evaluate_github_event(
                    "issue_comment",
                    comment_event(non_actionable_body),
                    live_pull_request=live_pr,
                )
                self.assertFalse(ignored.admitted)
                self.assertEqual(ignored.outcome_code, "no_sensei_mention")

        # Inline review comment URL must match https://api.github.com/repos/<owner>/<repo>/pulls/<n>.
        for bad_url in (
            "https://evil.example/repos/o/r/pulls/42",
            "http://api.github.com/repos/o/r/pulls/42",
            "https://api.github.com/repos/other/repo/pulls/42",
        ):
            with self.subTest(bad_url=bad_url):
                bad_inline = evaluate_github_event(
                    "pull_request_review_comment",
                    {
                        "action": "created",
                        "repository": {"full_name": "o/r"},
                        "comment": {
                            "id": 9010,
                            "body": "@sensei please check",
                            "updated_at": "2026-11-11T00:01:00Z",
                            "pull_request_url": bad_url,
                            "author_association": "MEMBER",
                            "user": {"login": "collab", "type": "User"},
                        },
                    },
                    live_pull_request=live_pr,
                )
                self.assertFalse(bad_inline.admitted)
                self.assertEqual(
                    bad_inline.outcome_code, "invalid_comment_pull_request_url"
                )
                self.assertIsNone(bad_inline.source_identity.pull_request_number)

        mismatched_pr_comment = evaluate_github_event(
            "issue_comment",
            comment_event("@reviewsensei verify"),
            live_pull_request=_pull_payload(number=99),
        )
        self.assertFalse(mismatched_pr_comment.admitted)
        self.assertEqual(
            mismatched_pr_comment.outcome_code, "pull_request_number_mismatch"
        )

    def test_unchanged_duplicate_events_create_zero_second_analysis_and_preserve_obligations(
        self,
    ) -> None:
        initial = evaluate_github_event(
            "pull_request",
            {"action": "opened", "pull_request": _pull_payload()},
        )
        self.assertTrue(initial.admitted)
        self.assertTrue(initial.requires_model_inference)

        comment_payload = {
            "action": "created",
            "issue": {
                "number": 42,
                "pull_request": {"url": "https://example/pulls/42"},
            },
            "comment": {
                "id": 777,
                "body": "@reviewsensei rescan",
                "updated_at": "2026-11-11T00:00:00Z",
                "author_association": "OWNER",
                "user": {"login": "owner", "type": "User"},
            },
        }
        first_comment = evaluate_github_event(
            "issue_comment",
            comment_payload,
            live_pull_request=_pull_payload(),
        )
        self.assertTrue(first_comment.admitted)
        assert first_comment.source_identity.source_dedup_key is not None

        retained = RetainedSnapshotState(
            reviewed_base_sha=BASE_SHA_1,
            reviewed_base_ref="main",
            reviewed_head_sha=HEAD_SHA_1,
            review_status="complete",
            open_finding_ids=("rs-open-1", "rs-open-2"),
            accepted_source_keys=frozenset(
                {first_comment.source_identity.source_dedup_key}
            ),
        )

        for action in ("synchronize", "reopened", "ready_for_review"):
            with self.subTest(duplicate_action=action):
                dup = evaluate_github_event(
                    "pull_request",
                    {"action": action, "pull_request": _pull_payload()},
                    retained_state=retained,
                )
                self.assertFalse(dup.admitted)
                self.assertEqual(dup.disposition, "reuse_retained_snapshot")
                self.assertEqual(dup.outcome_code, "unchanged_snapshot_reused")
                self.assertFalse(dup.requires_model_inference)
                self.assertTrue(dup.reused_retained_snapshot)
                self.assertEqual(
                    dup.preserved_open_finding_ids, ("rs-open-1", "rs-open-2")
                )

        dup_dispatch = evaluate_github_event(
            "workflow_dispatch",
            json.dumps({"inputs": {"pull_request_number": "42"}}),
            live_pull_request=_pull_payload(),
            retained_state=retained,
        )
        self.assertFalse(dup_dispatch.admitted)
        self.assertEqual(dup_dispatch.outcome_code, "unchanged_snapshot_reused")
        self.assertFalse(dup_dispatch.requires_model_inference)
        self.assertEqual(
            dup_dispatch.preserved_open_finding_ids, ("rs-open-1", "rs-open-2")
        )

        dup_comment = evaluate_github_event(
            "issue_comment",
            comment_payload,
            live_pull_request=_pull_payload(),
            retained_state=retained,
        )
        self.assertFalse(dup_comment.admitted)
        self.assertEqual(dup_comment.outcome_code, "duplicate_source_event_reused")
        self.assertFalse(dup_comment.requires_model_inference)
        self.assertEqual(
            dup_comment.preserved_open_finding_ids, ("rs-open-1", "rs-open-2")
        )

    def test_base_retarget_via_unsubscribed_edited_invalidates_reuse_and_recovers_cleanly(
        self,
    ) -> None:
        retained = RetainedSnapshotState(
            reviewed_base_sha=BASE_SHA_1,
            reviewed_base_ref="release/1.0",
            reviewed_head_sha=HEAD_SHA_1,
            review_status="complete",
            open_finding_ids=("rs-blocking-9",),
        )
        retargeted_pr = _pull_payload(
            base_sha=BASE_SHA_2,
            base_ref="main",
            head_sha=HEAD_SHA_1,
        )

        edited_event = evaluate_github_event(
            "pull_request",
            {
                "action": "edited",
                "changes": {
                    "base": {
                        "ref": {"from": "release/1.0"},
                        "sha": {"from": BASE_SHA_1},
                    }
                },
                "pull_request": retargeted_pr,
            },
            retained_state=retained,
        )
        self.assertFalse(edited_event.caller_subscribed)
        self.assertFalse(edited_event.admitted)
        self.assertEqual(edited_event.disposition, "read_time_invalidation")
        self.assertEqual(edited_event.outcome_code, "base_retargeted_invalidation")
        self.assertTrue(edited_event.invalidates_retained_baseline)
        self.assertFalse(edited_event.requires_model_inference)
        self.assertEqual(edited_event.recovery_command, "workflow_dispatch")
        self.assertEqual(edited_event.preserved_open_finding_ids, ("rs-blocking-9",))

        ref_only_pr = _pull_payload(
            base_sha=BASE_SHA_1,
            base_ref="main",
            head_sha=HEAD_SHA_1,
        )
        ref_only_edited = evaluate_github_event(
            "pull_request",
            {
                "action": "edited",
                "changes": {"base": {"ref": {"from": "release/1.0"}}},
                "pull_request": ref_only_pr,
            },
            retained_state=retained,
        )
        self.assertFalse(ref_only_edited.invalidates_retained_baseline)
        self.assertTrue(ref_only_edited.ref_only_base_retarget)
        self.assertEqual(
            ref_only_edited.outcome_code, "base_ref_retargeted_without_sha_change"
        )

        # Without retained_state, ref-only retargets with unchanged SHA do not claim SHA invalidation.
        ref_only_no_retained = evaluate_github_event(
            "pull_request",
            {
                "action": "edited",
                "changes": {
                    "base": {
                        "ref": {"from": "release/1.0"},
                        "sha": {"from": BASE_SHA_1},
                    }
                },
                "pull_request": ref_only_pr,
            },
            retained_state=None,
        )
        self.assertFalse(ref_only_no_retained.invalidates_retained_baseline)
        self.assertTrue(ref_only_no_retained.ref_only_base_retarget)
        self.assertEqual(
            ref_only_no_retained.outcome_code,
            "base_ref_retargeted_without_sha_change",
        )

        sha_changed_no_retained = evaluate_github_event(
            "pull_request",
            {
                "action": "edited",
                "changes": {
                    "base": {
                        "ref": {"from": "release/1.0"},
                        "sha": {"from": BASE_SHA_1},
                    }
                },
                "pull_request": retargeted_pr,
            },
            retained_state=None,
        )
        self.assertTrue(sha_changed_no_retained.invalidates_retained_baseline)
        self.assertEqual(
            sha_changed_no_retained.outcome_code, "base_retargeted_invalidation"
        )

        unknown_sha_no_retained = evaluate_github_event(
            "pull_request",
            {
                "action": "edited",
                "changes": {"base": {"ref": {"from": "release/1.0"}}},
                "pull_request": ref_only_pr,
            },
            retained_state=None,
        )
        self.assertFalse(unknown_sha_no_retained.invalidates_retained_baseline)
        self.assertFalse(unknown_sha_no_retained.ref_only_base_retarget)
        self.assertEqual(
            unknown_sha_no_retained.outcome_code, "base_changed_unknown_reuse"
        )

        recovery_dispatch = evaluate_github_event(
            "workflow_dispatch",
            {"inputs": {"pull_request_number": 42}},
            live_pull_request=retargeted_pr,
            retained_state=retained,
        )
        self.assertTrue(recovery_dispatch.admitted)
        self.assertEqual(recovery_dispatch.disposition, "admitted_review")
        self.assertEqual(recovery_dispatch.outcome_code, "manual_review_scheduled")
        self.assertFalse(recovery_dispatch.reused_retained_snapshot)
        self.assertTrue(recovery_dispatch.invalidates_retained_baseline)
        self.assertTrue(recovery_dispatch.requires_model_inference)
        self.assertEqual(
            recovery_dispatch.preserved_open_finding_ids, ("rs-blocking-9",)
        )
        assert recovery_dispatch.resolution is not None
        self.assertEqual(recovery_dispatch.resolution.base_sha, BASE_SHA_2)
        self.assertEqual(recovery_dispatch.resolution.head_sha, HEAD_SHA_1)

        self.assertEqual(
            evaluate_github_event(
                "workflow_dispatch",
                {"inputs": {"pull_request_number": "abc"}},
                live_pull_request=retargeted_pr,
            ).outcome_code,
            "invalid_workflow_dispatch_pr_number",
        )
        self.assertEqual(
            evaluate_github_event(
                "workflow_dispatch",
                {"inputs": {"pull_request_number": 42}},
                live_pull_request=None,
            ).outcome_code,
            "live_pull_request_unavailable",
        )
        self.assertEqual(
            evaluate_github_event(
                "workflow_dispatch",
                {"inputs": {"pull_request_number": 99}},
                live_pull_request=retargeted_pr,
            ).outcome_code,
            "pull_request_number_mismatch",
        )

    def test_installed_caller_subscriptions_map_to_real_trigger_webhook_and_action_routes(
        self,
    ) -> None:
        from argparse import Namespace

        from review_sensei_github import action_run

        from review_sensei.errors import ReviewInputError

        checked_in = (ROOT / ".github/workflows/review-sensei-review.yml").read_text(
            encoding="utf-8"
        )
        self.assertEqual(len(INSTALLED_CALLER_SUBSCRIPTION_ROUTES), 6)

        class _Secret:
            def load_webhook_secret(self) -> bytes:
                return b"g01-secret"

        verifier = WebhookVerifier(app_id=55, secret_source=_Secret())
        pull = _pull_payload()

        def _sign(obj: dict[str, Any]) -> tuple[bytes, str]:
            raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
            return (
                raw,
                "sha256=" + hmac.new(b"g01-secret", raw, hashlib.sha256).hexdigest(),
            )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            pull_file = tmp_dir / "pull.json"
            out_file = tmp_dir / "out.txt"
            pull_file.write_text(json.dumps(pull), encoding="utf-8")

            # 1. Subscribed supported routes map cleanly across YAML -> trigger.py -> webhooks.py -> action_run
            supported_cases = (
                (
                    "pr_opened",
                    "pull_request",
                    {"action": "opened", "pull_request": pull},
                    "",
                    "review",
                    "review_run.run_review",
                    True,
                ),
                (
                    "manual_dispatch",
                    "workflow_dispatch",
                    {"inputs": {"pull_request_number": 42}},
                    "",
                    "review",
                    "review_run.run_review",
                    True,
                ),
                (
                    "issue_rescan",
                    "issue_comment",
                    {
                        "action": "created",
                        "issue": {
                            "number": 42,
                            "pull_request": {
                                "url": "https://api.github.com/repos/owner/repo/pulls/42"
                            },
                        },
                        "comment": {
                            "id": 901,
                            "body": "@reviewsensei rescan",
                            "updated_at": "2026-11-11T00:00:00Z",
                            "author_association": "MEMBER",
                            "user": {"login": "alice", "type": "User"},
                        },
                    },
                    "@reviewsensei rescan",
                    "review",
                    "review_run.run_review",
                    True,
                ),
                (
                    "issue_command",
                    "issue_comment",
                    {
                        "action": "created",
                        "issue": {
                            "number": 42,
                            "pull_request": {
                                "url": "https://api.github.com/repos/owner/repo/pulls/42"
                            },
                        },
                        "comment": {
                            "id": 902,
                            "body": "@reviewsensei verify",
                            "updated_at": "2026-11-11T00:00:00Z",
                            "author_association": "OWNER",
                            "user": {"login": "alice", "type": "User"},
                        },
                    },
                    "@reviewsensei verify",
                    "command",
                    "action_run.run_action(purpose='command')",
                    False,
                ),
                (
                    "inline_reply",
                    "pull_request_review_comment",
                    {
                        "action": "created",
                        "pull_request": pull,
                        "comment": {
                            "id": 903,
                            "body": "@reviewsensei why?",
                            "updated_at": "2026-11-11T00:00:00Z",
                            "pull_request_url": "https://api.github.com/repos/owner/repo/pulls/42",
                            "author_association": "COLLABORATOR",
                            "user": {"login": "alice", "type": "User"},
                        },
                    },
                    "@reviewsensei why?",
                    "reply",
                    "action_run.run_action(purpose='reply')",
                    True,
                ),
            )

            for (
                label,
                ev_name,
                payload,
                c_body,
                expected_op,
                expected_entry,
                needs_inf,
            ) in supported_cases:
                with self.subTest(supported=label):
                    mapped = map_installed_caller_subscription(
                        checked_in,
                        event_name=ev_name,
                        payload=payload,
                        live_pull_request=pull,
                    )
                    self.assertTrue(mapped.caller_job_admitted)
                    self.assertEqual(mapped.routing.operation, expected_op)
                    self.assertEqual(
                        mapped.reusable_workflow_entrypoint, expected_entry
                    )
                    self.assertTrue(mapped.performs_native_effects)
                    self.assertEqual(mapped.requires_model_inference, needs_inf)

                    out_file.write_text("", encoding="utf-8")
                    with patch.dict(
                        os.environ, {"GITHUB_OUTPUT": str(out_file)}, clear=False
                    ):
                        rc = hosting_trigger.main(
                            [
                                "--event",
                                ev_name,
                                "--comment-body",
                                c_body,
                                "--pull-json",
                                str(pull_file),
                            ]
                        )
                    self.assertEqual(rc, 0)
                    cli_kv = dict(
                        line.split("=", 1)
                        for line in out_file.read_text(encoding="utf-8").splitlines()
                    )
                    self.assertEqual(cli_kv["operation"], expected_op)

                    raw, sig = _sign(payload)
                    verified = verifier.verify_event_route(
                        body=raw,
                        signature=sig,
                        event=ev_name,
                        delivery_id=f"deliv-{label}",
                        live_pull_request=pull,
                    )
                    self.assertTrue(verified.routing.admitted)
                    self.assertEqual(verified.routing.operation, expected_op)

            # 2. Explicit no-op and unsupported event classes across all boundaries
            unsupported_or_noop = (
                (
                    "pr_edited",
                    "pull_request",
                    {"action": "edited", "pull_request": pull},
                    False,
                ),
                (
                    "pr_closed",
                    "pull_request",
                    {"action": "closed", "pull_request": pull},
                    False,
                ),
                (
                    "comment_edited",
                    "issue_comment",
                    {
                        "action": "edited",
                        "issue": {"number": 42, "pull_request": {}},
                        "comment": {
                            "id": 910,
                            "body": "@reviewsensei verify",
                            "updated_at": "2026-11-11T00:01:00Z",
                            "author_association": "OWNER",
                            "user": {"login": "alice", "type": "User"},
                        },
                    },
                    False,
                ),
                (
                    "bot_comment",
                    "issue_comment",
                    {
                        "action": "created",
                        "issue": {
                            "number": 42,
                            "pull_request": {
                                "url": "https://api.github.com/repos/owner/repo/pulls/42"
                            },
                        },
                        "comment": {
                            "id": 911,
                            "body": "@reviewsensei verify",
                            "updated_at": "2026-11-11T00:01:00Z",
                            "author_association": "OWNER",
                            "user": {"login": "bot[bot]", "type": "Bot"},
                        },
                    },
                    True,
                ),
                (
                    "merge_group",
                    "merge_group",
                    {"action": "checks_requested"},
                    False,
                ),
                (
                    "thread_resolved",
                    "pull_request_review_thread",
                    {"action": "resolved", "pull_request": {"number": 42}},
                    False,
                ),
                (
                    "human_review",
                    "pull_request_review",
                    {
                        "action": "submitted",
                        "pull_request": pull,
                        "review": {
                            "state": "approved",
                            "commit_id": HEAD_SHA_1,
                            "user": {"login": "bob", "type": "User"},
                        },
                    },
                    False,
                ),
            )
            for label, ev_name, payload, expected_sub in unsupported_or_noop:
                with self.subTest(noop_or_unsupported=label):
                    mapped = map_installed_caller_subscription(
                        checked_in,
                        event_name=ev_name,
                        payload=payload,
                        live_pull_request=pull,
                    )
                    self.assertEqual(mapped.routing.caller_subscribed, expected_sub)
                    self.assertFalse(mapped.caller_job_admitted)
                    self.assertIsNone(mapped.reusable_workflow_entrypoint)
                    self.assertFalse(mapped.performs_native_effects)
                    self.assertFalse(mapped.requires_model_inference)

            # Edited comment event reaching action_run.event_source is refused fail-closed
            ev_path = tmp_dir / "edited-comment.json"
            ev_path.write_text(json.dumps(unsupported_or_noop[2][2]), encoding="utf-8")
            with (
                patch.dict(
                    os.environ,
                    {
                        "GITHUB_EVENT_NAME": "issue_comment",
                        "GITHUB_EVENT_PATH": str(ev_path),
                    },
                    clear=False,
                ),
                self.assertRaisesRegex(ReviewInputError, "source_edited"),
            ):
                action_run.event_source(
                    Namespace(
                        pull_request=42,
                        source_comment_id=910,
                        comment_body="@reviewsensei verify",
                        source_updated_at="2026-11-11T00:01:00Z",
                    ),
                    purpose="command",
                )

            # Unapproved caller workflow triggers raise ValueError in map_installed_caller_subscription
            drifted_workflow = checked_in.replace(
                "  pull_request:\n",
                "  merge_group: [checks_requested]\n  pull_request:\n",
            )
            with self.assertRaises(ValueError):
                map_installed_caller_subscription(
                    drifted_workflow,
                    event_name="pull_request",
                    payload={"action": "opened", "pull_request": pull},
                )


if __name__ == "__main__":
    unittest.main()
