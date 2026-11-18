"""Native bindings must come from exact owned readback, not review arithmetic."""

from __future__ import annotations

import unittest
from copy import deepcopy
from dataclasses import replace

from review_sensei_github.publication import (
    ReviewPublisher,
    _native_finding_receipts_from_nodes,
)

from review_sensei.active_finding_window import (
    commit_window_publication,
    evaluate_active_window_round,
    reconcile_uncertain_slot,
)
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.errors import ReviewInputError
from review_sensei.finding_drift import (
    NativeThreadBinding,
    commit_drift_publication,
    evaluate_drift_round,
)
from review_sensei.human_assessment import finding_instance_fingerprint
from tests.test_active_window_lifecycle_g10 import (
    BASE_SHA,
    HEAD_1,
    ORDINARY_DIFF,
    _comment,
    _result,
    _WindowGitHubHost,
)


def _native_fixture():
    host = _WindowGitHubHost(head_sha=HEAD_1)
    publisher = ReviewPublisher(http=host.http)
    comment = _comment(line=5)
    result = _result(comment)
    publisher.publish(
        token="test",
        repository="owner/repo",
        repository_id=1,
        pull_request=1,
        head_sha=HEAD_1,
        base_branch="main",
        base_sha=BASE_SHA,
        result=result,
        diff=ORDINARY_DIFF,
        app_slug=host.app_slug,
        max_active_findings=1,
        convergence_policy=ReviewConvergencePolicy(mode="strict"),
    )
    node = deepcopy(host.threads[0])
    node["id"] = "PRRT_opaque_service_assigned_Q"
    node["comments"]["nodes"][0]["databaseId"] = 768431
    body = node["comments"]["nodes"][0]["body"]
    instance = finding_instance_fingerprint(comment)
    return (
        node,
        instance,
        dict(
            repository_id=1,
            pull_request=1,
            head_sha=HEAD_1,
            base_sha=BASE_SHA,
            producer_id=9001,
            review_id=host.reviews[0]["id"],
            app_slug=host.app_slug,
            expected={instance: body},
            expected_anchors={instance: (comment.path, comment.line)},
        ),
    )


class NativeQualificationTests(unittest.TestCase):
    def test_receipt_keeps_real_native_ids_and_exact_scope(self):
        node, instance, scope = _native_fixture()
        receipt = _native_finding_receipts_from_nodes([node], **scope)[instance]
        assert receipt.thread_id == "PRRT_opaque_service_assigned_Q"
        assert receipt.comment_id == 768431
        assert receipt.review_id == 100
        assert receipt.producer_id == 9001
        assert len(receipt.proof_sha256) == 64

    def test_unselected_historical_root_does_not_poison_current_receipt(self):
        node, instance, scope = _native_fixture()
        historical = deepcopy(node)
        historical["id"] = "PRRT_historical"
        root = historical["comments"]["nodes"][0]
        root["databaseId"] += 1
        root["body"] = root["body"].replace(HEAD_1, "e" * 40)
        root["commit"]["oid"] = "e" * 40
        root["pullRequestReview"]["databaseId"] += 1
        historical["isResolved"] = True
        for nodes in ([historical, node], [node, historical]):
            with self.subTest(order=[item["id"] for item in nodes]):
                receipt = _native_finding_receipts_from_nodes(nodes, **scope)[instance]
                self.assertEqual(receipt.thread_id, node["id"])
                self.assertEqual(receipt.head_sha, HEAD_1)
                # An unrelated historical root must not hide current ambiguity
                # or allow a forged current root to establish authority.
                self.assertNotIn(
                    instance,
                    _native_finding_receipts_from_nodes(
                        [*nodes, deepcopy(node)], **scope
                    ),
                )
                forged = deepcopy(node)
                forged["comments"]["nodes"][0]["author"]["databaseId"] += 1
                self.assertNotIn(
                    instance,
                    _native_finding_receipts_from_nodes([historical, forged], **scope),
                )

    def test_selected_reused_root_keeps_native_head_and_exact_scope_checks(self):
        node, instance, scope = _native_fixture()
        root = node["comments"]["nodes"][0]
        original_head = "e" * 40
        root["commit"]["oid"] = original_head
        scope["existing_bindings"] = {
            instance: {
                "thread_id": node["id"],
                "comment_id": root["databaseId"],
                "review_id": root["pullRequestReview"]["databaseId"],
                "native_head_sha": original_head,
            }
        }
        receipt = _native_finding_receipts_from_nodes([node], **scope)[instance]
        self.assertEqual(receipt.native_head_sha, original_head)
        self.assertEqual(receipt.head_sha, HEAD_1)
        # Explicit selection cannot silently discard an old marker/body and
        # switch to a different newly observed root for the same instance.
        historical = deepcopy(node)
        historical["comments"]["nodes"][0]["body"] = root["body"].replace(
            HEAD_1, original_head
        )
        replacement = deepcopy(node)
        replacement["id"] = "PRRT_unselected_replacement"
        replacement["comments"]["nodes"][0]["databaseId"] += 1
        for nodes in ([historical, replacement], [replacement, historical]):
            self.assertNotIn(
                instance, _native_finding_receipts_from_nodes(nodes, **scope)
            )

    def test_mutated_or_ambiguous_readback_does_not_confirm(self):
        for mutation in [
            "body",
            "producer",
            "comment",
            "review",
            "head",
            "anchor",
            "resolved",
            "duplicate",
        ]:
            with self.subTest(mutation=mutation):
                node, instance, scope = _native_fixture()
                root = node["comments"]["nodes"][0]
                if mutation == "body":
                    root["body"] += " edited"
                elif mutation == "producer":
                    root["author"]["databaseId"] = 12
                elif mutation == "comment":
                    root["databaseId"] = True
                elif mutation == "review":
                    root["pullRequestReview"]["databaseId"] += 1
                elif mutation == "head":
                    root["commit"]["oid"] = "f" * 40
                elif mutation == "anchor":
                    root["path"] = "wrong/path.py"
                elif mutation == "resolved":
                    node["isResolved"] = True
                nodes = [node, deepcopy(node)] if mutation == "duplicate" else [node]
                assert instance not in _native_finding_receipts_from_nodes(
                    nodes, **scope
                )

    def test_review_id_alone_never_confirms_a_window_or_drift_binding(self):
        host = _WindowGitHubHost()
        publisher = ReviewPublisher(http=host.http)
        state = evaluate_active_window_round(
            _result(_comment()),
            head_sha=HEAD_1,
            base_sha=BASE_SHA,
            max_active_findings=1,
            round_number=1,
            coverage_mode="full",
        )
        committed = publisher._commit_active_window_success(
            repository_id=1,
            pull_request=1,
            head_sha=HEAD_1,
            base_sha=BASE_SHA,
            planned_state=state,
            input_key="b" * 64,
            review_id=45678,
        )
        assert committed.display_active_findings.slots[0].state == "uncertain"
        assert committed.display_active_findings.slots[0].target.startswith("intent:")
        drift = evaluate_drift_round(
            _result(_comment(line=5)),
            head_sha=HEAD_1,
            base_sha=BASE_SHA,
            max_active_findings=1,
            round_number=1,
            coverage_mode="full",
            diff_text=ORDINARY_DIFF,
            convergence_policy=ReviewConvergencePolicy(mode="strict"),
        )
        committed_drift = publisher._commit_drift_success(
            repository_id=1,
            pull_request=1,
            head_sha=HEAD_1,
            base_sha=BASE_SHA,
            planned_drift=drift,
            input_key="b" * 64,
            review_id=45678,
        )
        binding = committed_drift.inventory.obligations[0].thread_binding
        assert binding.slot_state == "uncertain"
        assert binding.comment_id is None
        assert binding.thread_id.startswith("intent:")
        again = commit_drift_publication(committed_drift)
        assert again.inventory.obligations[0].thread_binding == binding

    def test_pending_intent_cannot_be_promoted_without_a_host_identity(self):
        binding = NativeThreadBinding(
            "intent:" + "a" * 64, None, "inline", None, "uncertain", "b" * 64
        )
        with self.assertRaises(ReviewInputError):
            replace(binding, slot_state="active")
        with self.assertRaises(ReviewInputError):
            replace(binding, comment_id=123)

    def test_confirmed_window_readback_rebinds_intent_without_losing_history(self):
        state = evaluate_active_window_round(
            _result(_comment()),
            head_sha=HEAD_1,
            base_sha=BASE_SHA,
            max_active_findings=1,
            round_number=1,
            coverage_mode="full",
        )
        oid = state.authoritative_decision_inventory.obligations[0].obligation_id
        state = commit_window_publication(
            state, uncertain_targets={oid: "intent:pending"}
        )
        result = reconcile_uncertain_slot(
            state,
            target="intent:pending",
            confirmed_target="PRRT_real_native",
            resolved_state="active",
            proof_sha256="c" * 64,
        )
        assert (
            result.authoritative_decision_inventory.obligations[0].native_target
            == "PRRT_real_native"
        )
        assert {(s.target, s.state) for s in result.display_active_findings.slots} == {
            ("intent:pending", "released"),
            ("PRRT_real_native", "active"),
        }

    def test_complete_review_replay_accepts_exact_retained_old_native_membership(self):
        from tests.test_finding_location_drift_g16 import HEAD_2, _DriftGitHubHost
        from tests.test_finding_location_drift_g16 import _comment as drift_comment
        from tests.test_finding_location_drift_g16 import _result as drift_result

        host = _DriftGitHubHost()
        publisher = ReviewPublisher(http=host.http)
        arguments = dict(
            token="test",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            base_branch="main",
            base_sha=BASE_SHA,
            diff=ORDINARY_DIFF,
            app_slug=host.app_slug,
            enable_drift_tracking=True,
            max_active_findings=1,
            complete_inventory=True,
            convergence_policy=ReviewConvergencePolicy(mode="strict"),
        )
        result = drift_result(
            drift_comment("src/app.py", line=5), paths=("src/app.py",)
        )
        assert (
            publisher.publish(head_sha=HEAD_1, result=result, **arguments).status
            == "published"
        )
        first = publisher.drift_state(
            repository_id=1, pull_request=1
        ).inventory.obligations[0]
        host.head_sha = HEAD_2
        assert (
            publisher.publish(head_sha=HEAD_2, result=result, **arguments).status
            == "published"
        )
        assert len(host.threads) == 1
        second = publisher.drift_state(
            repository_id=1, pull_request=1
        ).inventory.obligations[0]
        assert second.thread_binding.comment_id == first.thread_binding.comment_id
        assert second.thread_binding.thread_id == first.thread_binding.thread_id
        effects = [call for call in host.calls if call[0] == "PATCH"]
        assert len(effects) == 1
        assert (
            publisher.publish(head_sha=HEAD_2, result=result, **arguments).status
            == "already_published"
        )
        assert len(host.reviews) == 2
        assert [call for call in host.calls if call[0] == "PATCH"] == effects
        receipts = publisher.native_finding_receipts(repository_id=1, pull_request=1)
        assert any(
            (
                r["head_sha"] == HEAD_2
                and r["native_head_sha"] == HEAD_1
                and (r["review_id"] == host.reviews[0]["id"])
                for r in receipts
            )
        )

    def test_human_edit_of_retained_root_is_not_overwritten_or_recreated(self):
        from tests.test_finding_location_drift_g16 import HEAD_2, _DriftGitHubHost
        from tests.test_finding_location_drift_g16 import _comment as drift_comment
        from tests.test_finding_location_drift_g16 import _result as drift_result

        host = _DriftGitHubHost()
        publisher = ReviewPublisher(http=host.http)
        arguments = dict(
            token="test",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            base_branch="main",
            base_sha=BASE_SHA,
            diff=ORDINARY_DIFF,
            app_slug=host.app_slug,
            enable_drift_tracking=True,
            max_active_findings=1,
            convergence_policy=ReviewConvergencePolicy(mode="strict"),
        )
        result = drift_result(
            drift_comment("src/app.py", line=5), paths=("src/app.py",)
        )
        assert (
            publisher.publish(head_sha=HEAD_1, result=result, **arguments).status
            == "published"
        )
        host.threads[0]["comments"]["nodes"][0]["body"] += " edited by a maintainer"
        host.head_sha = HEAD_2
        outcome = publisher.publish(head_sha=HEAD_2, result=result, **arguments)
        assert outcome.status == "publication_incomplete"
        assert len(host.reviews) == len(host.threads) == 1
        assert not [call for call in host.calls if call[0] == "PATCH"]
        state = publisher.drift_state(repository_id=1, pull_request=1)
        assert state.inventory.obligations[0].thread_binding.slot_state == "uncertain"

    def test_full_edit_batch_capacity_preview_never_spends_original_tail(self):
        from review_sensei_github.quarantine_capacity import NativeCapacityGuard

        from review_sensei.session import SessionIdentity

        reserve = {
            surface: dict(
                new_objects=0,
                new_wire_bytes=0,
                positive_update_bytes=0,
                max_object_wire_bytes=0,
            )
            for surface in (
                "issue_comments",
                "inline_replies",
                "reviews",
                "checks",
                "threads",
                "reactions",
            )
        }
        guard = NativeCapacityGuard(
            identity=SessionIdentity("owner/repo", 1, 1), reserve=reserve
        )
        before = deepcopy(guard.__dict__)
        small = (
            "PATCH",
            "/repos/owner/repo/pulls/comments/73001",
            {"body": "exact update"},
        )
        guard.preflight_native_edits([small])
        assert guard.__dict__ == before
        oversized = (
            "PATCH",
            "/repos/owner/repo/pulls/comments/73002",
            {"body": "x" * (512 * 1024)},
        )
        with self.assertRaises(ReviewInputError):
            guard.preflight_native_edits([small, oversized])
        assert guard.__dict__ == before
        guard.before_request(*small)
        assert guard.stats != before["stats"]

    def test_authenticated_resolution_discharge_is_exact_and_keeps_hidden_blocker(self):
        from types import SimpleNamespace

        from review_sensei_github.conversation import (
            apply_authenticated_finding_resolution,
        )

        first = _comment(line=5)
        second = _comment(path="src/other.py", line=8, body="Different required fix.")
        state = evaluate_active_window_round(
            _result(first, second),
            head_sha=HEAD_1,
            base_sha=BASE_SHA,
            max_active_findings=1,
            round_number=1,
            coverage_mode="full",
        )
        finalizer = SimpleNamespace(
            _active_window_states={(1, 1): state}, _drift_states={}
        )
        apply_authenticated_finding_resolution(
            finalizer,
            repository_id=1,
            pull_request=1,
            head_sha=HEAD_1,
            instances=("f" * 64,),
            evidence_sha256="e" * 64,
        )
        self.assertEqual(finalizer._active_window_states[(1, 1)], state)
        displayed = state.display_active_findings.entries[0].obligation_id
        selected = next(
            item
            for item in state.authoritative_decision_inventory.obligations
            if item.obligation_id == displayed
        )
        apply_authenticated_finding_resolution(
            finalizer,
            repository_id=1,
            pull_request=1,
            head_sha=HEAD_1,
            instances=(finding_instance_fingerprint(selected.comment),),
            evidence_sha256="e" * 64,
        )
        updated = finalizer._active_window_states[(1, 1)]
        obligations = updated.authoritative_decision_inventory.obligations
        self.assertEqual(sum(item.unresolved for item in obligations), 1)
        fixed = next(item for item in obligations if item.obligation_id == displayed)
        self.assertEqual(fixed.lifecycle_state, "verified-fixed")
        self.assertEqual(fixed.resolution_criterion, selected.resolution_criterion)
        self.assertEqual(fixed.comment.to_dict(), selected.comment.to_dict())
        self.assertIsNotNone(fixed.resolution_proof_sha256)

    def test_preinference_reply_receipt_authenticates_delivery_not_decision(self):
        from types import SimpleNamespace

        from review_sensei_github.conversation import (
            ConversationPublisher,
            _digest,
            reply_marker,
        )
        from review_sensei_github.errors import GitHubConversationError

        marker = reply_marker(
            source_comment_id=17,
            source_updated_digest=_digest("timestamp"),
            pull_request=1,
            head_sha=HEAD_1,
        )
        producer = {"id": 9001, "type": "Bot", "login": "reviewsensei[bot]"}
        row = {
            "id": 29,
            "body": "A natural-language response.\n\n" + marker,
            "user": producer,
            "pull_request_url": "https://api.github.test/repos/owner/repo/pulls/1",
            "in_reply_to_id": 11,
        }

        class HTTP:
            api_url = "https://api.github.test"

            def request(self, *args, **kwargs):
                return 200, producer

            def repository_path(self, repository, suffix):
                return f"/repos/{repository}{suffix}"

            def paginate(self, **kwargs):
                return self.rows

        http = HTTP()
        publisher = ConversationPublisher(http=http)
        prepared = SimpleNamespace(
            source_comment_id=17,
            source_updated_at="timestamp",
            head_sha=HEAD_1,
            source_kind="inline",
            root_comment_id=11,
        )
        args = dict(
            token="test",
            repository="owner/repo",
            pull_request=1,
            prepared=prepared,
            app_slug=producer["login"],
        )
        http.rows = [row]
        outcome = publisher.existing_replied(**args)
        self.assertEqual(outcome.status, "already_replied")
        self.assertFalse(outcome.resolved)
        self.assertEqual(outcome.approval_status, "approval_withheld")
        content = "Bounded pending response."
        page_marker = (
            marker[:-4]
            + " report="
            + "a" * 64
            + " part=1/1 content="
            + _digest(content)
            + " -->"
        )
        http.rows = [{**row, "body": content + "\n\n" + page_marker}]
        self.assertEqual(
            publisher.existing_replied(**args, producer_id=9001).status,
            "already_replied",
        )
        http.rows = [{**row, "body": "edited" + content + "\n\n" + page_marker}]
        with self.assertRaises(GitHubConversationError):
            publisher.existing_replied(**args, producer_id=9001)
        for changed in (
            {"user": {**producer, "id": 9002}},
            {"body": row["body"].replace(HEAD_1, "f" * 40)},
        ):
            with self.subTest(changed=changed):
                http.rows = [{**row, **changed}]
                self.assertIsNone(publisher.existing_replied(**args))
        for rows in (
            [row, row],
            [{**row, "in_reply_to_id": 12}],
            [{**row, "body": row["body"] + " edited"}],
        ):
            with self.subTest(rows=rows):
                http.rows = rows
                with self.assertRaises(GitHubConversationError):
                    publisher.existing_replied(**args)
