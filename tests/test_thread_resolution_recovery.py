"""Public reply/verify recovery and scoped thread mutation regressions."""

import copy
import unittest
from dataclasses import replace

from review_sensei_github.approval import approval_eligibility_from_result
from review_sensei_github.conversation import ConversationPublisher
from review_sensei_github.errors import GitHubHTTPTransientError, GitHubPublicationError
from review_sensei_github.http import GitHubHttp
from review_sensei_github.publication import (
    IndexedFinding,
    ReviewApprovalFinalizer,
    ReviewPublisher,
    approval_eligibility_marker,
    finding_index_marker,
    finding_instance_marker,
    finding_marker,
)
from review_sensei_github.thread_resolution import (
    RESOLUTION_PREFIX,
    FindingThreadResolver,
)

from review_sensei.context import finding_lifecycle_for_comment
from review_sensei.disposition import FindingDisposition
from review_sensei.human_assessment import finding_instance_fingerprint
from review_sensei.models import ConversationReply, ReviewComment, ReviewResult
from tests.test_github_publication import pr_payload

HEAD, BASE = "b" * 40, "a" * 40
APP = "reviewsensei[bot]"


class ResolutionHost(GitHubHttp):
    """Stateful GitHub transport: keeps durable records across publisher instances."""

    def __init__(self, *, human=False):
        super().__init__(api_url="https://api.github.test")
        self.calls = []
        self.comment = ReviewComment(
            path="src/app.py",
            line=2,
            body="Validate input",
            blocking=not human,
            needs_human=human,
        )
        self.result = ReviewResult(
            summary="Summary",
            comments=(self.comment,),
            provider="ollama",
            review_status="complete",
        )
        self.instance = finding_instance_fingerprint(self.comment)
        self.concern = finding_lifecycle_for_comment(self.comment).fingerprint
        self.eligibility = approval_eligibility_from_result(
            self.result,
            head_sha=HEAD,
            base_sha=BASE,
            enabled=True,
            app_authored=False,
            qualification="not-required",
            check_published=True,
        )
        self.eligibility = replace(
            self.eligibility,
            facts=replace(self.eligibility.facts, has_blocking_findings=False),
        )
        self.index = finding_index_marker(
            repository_id=1,
            pull_request=2,
            head_sha=HEAD,
            base_sha=BASE,
            result_digest=self.eligibility.result_digest,
            entries=(
                IndexedFinding(
                    instance=self.instance,
                    lifecycle=self.concern,
                    blocking=not human,
                    needs_human=human,
                    body_placed=False,
                ),
            ),
        )
        self.reviews = [
            self.review(
                5, approval_eligibility_marker(self.eligibility) + "\n" + self.index
            )
        ]
        self.root = {
            "id": 10,
            "pull_request_url": self.api_url + "/repos/owner/repo/pulls/2",
            "user": {"login": APP, "type": "Bot"},
            "in_reply_to_id": None,
            "body": finding_marker(
                repository_id=1,
                pull_request=2,
                head_sha=HEAD,
                base_sha=BASE,
                result=self.result,
                blocking=not human,
                fingerprint=self.concern,
                needs_human=human,
            )
            + "\n"
            + finding_instance_marker(self.comment),
        }
        self.source = {
            "id": 11,
            "pull_request_url": self.root["pull_request_url"],
            "user": {"login": "alice", "type": "User"},
            "author_association": "MEMBER",
            "in_reply_to_id": 10,
            "body": "@reviewsensei addressed this",
            "updated_at": "2026-10-13T00:00:00Z",
            "path": "src/app.py",
        }
        self.comments = [self.root, self.source]
        self.pr = pr_payload(head_sha=HEAD)
        self.resolved = False
        self.graphql_author = None
        self.fail_mutation = self.fail_approval = self.fail_record = False
        self.lookup_failure = None
        self.before_resolution = None
        self.missing_thread = False
        self.next_id = 20

    def review(self, id, body, state="COMMENTED"):
        return {
            "id": id,
            "commit_id": HEAD,
            "user": {"login": APP, "type": "Bot"},
            "body": body,
            "state": state,
        }

    def request(self, method, path, *, token, body=None, **kwargs):
        self.calls.append((method, path, copy.deepcopy(body)))
        status, value = self.respond(method, path.split("?")[0], body)
        return status, copy.deepcopy(value)

    def respond(self, method, path, body):
        if path == "/graphql":
            op = body["operationName"]
            if op == "ResolutionReadback":
                return 200, {
                    "data": {
                        "node": {"id": "PRRT_current", "isResolved": self.resolved}
                    }
                }
            if op == "ResolveReviewThread":
                if self.fail_mutation:
                    return 500, {}
                self.resolved = True
                return 200, {
                    "data": {
                        "resolveReviewThread": {
                            "thread": {"id": "PRRT_current", "isResolved": True}
                        }
                    }
                }
            if op == "FindingResolutionThreads":
                if self.before_resolution:
                    self.before_resolution(self)
                if self.lookup_failure == "transport":
                    raise GitHubHTTPTransientError("lookup unavailable")
                if self.lookup_failure == "malformed":
                    return 200, {}
            node = {
                "id": "PRRT_current",
                "isResolved": self.resolved,
                "comments": {
                    "nodes": [
                        {
                            "databaseId": 10,
                            "body": self.root["body"],
                            "author": self.graphql_author
                            or {"login": self.root["user"]["login"]},
                        }
                    ]
                },
            }
            return 200, {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "nodes": [] if self.missing_thread else [node],
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                            }
                        }
                    }
                }
            }
        if path == "/repos/owner/repo/pulls/2":
            return 200, self.pr
        if path == "/repos/owner/repo/pulls/comments/10":
            return 200, self.root
        if path == "/repos/owner/repo/pulls/comments/11":
            return 200, self.source
        if path == "/repos/owner/repo/pulls/2/comments":
            return 200, self.comments
        if path == "/repos/owner/repo/pulls/2/comments/10/replies":
            self.next_id += 1
            comment = {
                "id": self.next_id,
                "body": body["body"],
                "user": {"login": APP, "type": "Bot"},
                "in_reply_to_id": 10,
            }
            self.comments.append(comment)
            return 201, comment
        if path.startswith("/repos/owner/repo/pulls/2/reviews"):
            if method == "PATCH":
                return 405, None
            if method == "PUT":
                review = next(
                    item
                    for item in self.reviews
                    if item["id"] == int(path.rsplit("/", 1)[-1])
                )
                review["body"] = body["body"]
                return 200, review
            if method == "POST":
                if (self.fail_record and RESOLUTION_PREFIX in body["body"]) or (
                    self.fail_approval and body["event"] == "APPROVE"
                ):
                    return 500, {}
                self.next_id += 1
                review = self.review(
                    self.next_id,
                    body["body"],
                    "APPROVED" if body["event"] == "APPROVE" else "COMMENTED",
                )
                self.reviews.append(review)
                return 200, review
            return 200, self.reviews
        raise AssertionError((method, path, body))

    def reply(self):
        return ConversationPublisher(http=self).publish_current(
            token="token",
            repository="owner/repo",
            pull_request=2,
            source_comment_id=11,
            source_updated_at=self.source["updated_at"],
            head_sha=HEAD,
            app_slug=APP,
            root_comment_id=10,
            reply=ConversationReply("The current diff addresses this finding.", True),
            resolution_evidence="ef" * 32,
            resolution_token="review",
            resolution_base_sha=BASE,
        )

    def verify(self, dispositions=()):
        return ReviewApprovalFinalizer(http=self).refinalize_with_dispositions(
            token="token",
            check_token=None,
            repository="owner/repo",
            repository_id=1,
            pull_request=2,
            head_sha=HEAD,
            app_slug=APP,
            actor="alice",
            finding_ids=(),
            dispositions=dispositions,
        )


class ThreadResolutionDenialTests(unittest.TestCase):
    def resolve(self, response, *, already_resolved=()):
        from tests.fake_github_http import json_response
        from tests.test_command_capacity_recovery import CommandCapacityState

        state = CommandCapacityState(
            schema="1", inline_range=(0, 3), human_count=3, padded=False
        )
        state.resolved_threads = set(already_resolved)
        operations = []

        def opener(request, timeout):
            if request.full_url.endswith("/graphql"):
                import json

                body = json.loads(request.data)
                operations.append((body["operationName"], body["variables"]))
                scripted = response(state, body)
                if scripted is not None:
                    return json_response(*scripted)
            return state.open(request, timeout)

        result = FindingThreadResolver(
            GitHubHttp(api_url="https://api.github.test", opener=opener)
        ).resolve(
            token="synthetic",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha=HEAD,
            base_sha=BASE,
            app_slug=APP,
            instances=frozenset(e.instance for e in state.entries[:3]),
            fence=lambda: True,
            skip_resolved=True,
        )
        return state, result, operations

    def test_known_permission_denials_stop_without_observer_or_cleanup_budget(self):
        denials = (
            ({}, 401),
            ({}, 403),
            ({"errors": [{"type": "UNAUTHORIZED"}]}, 200),
            (
                {
                    "errors": [{"type": []}, {"type": "FORBIDDEN"}],
                    "data": {
                        "resolveReviewThread": {
                            "thread": {"id": "PRRT_100", "isResolved": True}
                        }
                    },
                },
                200,
            ),
        )
        for denial in denials:
            with self.subTest(response=denial):
                state, result, operations = self.resolve(
                    lambda state, body: (
                        denial
                        if body["operationName"] == "ResolveReviewThread"
                        else None
                    )
                )
                self.assertEqual(
                    [op for op, _ in operations],
                    ["FindingResolutionThreads", "ResolveReviewThread"],
                )
                self.assertEqual(result.resolved, ())
                self.assertEqual(
                    set(result.unconfirmed), {e.instance for e in state.entries[:3]}
                )
                self.assertEqual(state.resolved_threads, set())

    def test_denial_preserves_already_resolved_suffix_without_more_mutations(self):
        state, result, operations = self.resolve(
            lambda state, body: (
                ({"errors": [{"type": "FORBIDDEN"}]}, 200)
                if body["operationName"] == "ResolveReviewThread"
                else None
            ),
            already_resolved=(102,),
        )
        self.assertEqual(result.resolved, (state.entries[2].instance,))
        self.assertEqual(
            set(result.unconfirmed), {e.instance for e in state.entries[:2]}
        )
        self.assertEqual([op for op, _ in operations].count("ResolveReviewThread"), 1)
        self.assertEqual(state.resolved_threads, {102})

    def test_denied_readback_after_lost_response_stops_remaining_mutations(self):
        def response(state, body):
            if body["operationName"] == "ResolveReviewThread":
                raise GitHubHTTPTransientError("response unavailable")
            if body["operationName"] == "ResolutionReadback":
                return {"errors": [{"type": "FORBIDDEN"}]}, 200

        state, result, operations = self.resolve(response)
        self.assertEqual(
            [op for op, _ in operations],
            ["FindingResolutionThreads", "ResolveReviewThread", "ResolutionReadback"],
        )
        self.assertEqual(result.resolved, ())
        self.assertEqual(
            set(result.unconfirmed), {e.instance for e in state.entries[:3]}
        )
        self.assertEqual(state.resolved_threads, set())

    def test_non_permission_failure_preserves_bounded_readback_and_other_progress(self):
        def response(state, body):
            if (
                body["operationName"] == "ResolveReviewThread"
                and body["variables"]["input"]["threadId"] == "PRRT_100"
            ):
                return {"errors": [{"type": "INTERNAL"}]}, 200

        state, result, operations = self.resolve(response)
        self.assertEqual(result.unconfirmed, (state.entries[0].instance,))
        self.assertEqual(set(result.resolved), {e.instance for e in state.entries[1:3]})
        self.assertEqual(state.resolved_threads, {101, 102})
        self.assertEqual([op for op, _ in operations].count("ResolutionReadback"), 1)
        self.assertEqual([op for op, _ in operations].count("ResolveReviewThread"), 3)

    def test_lost_response_confirmed_by_readback_does_not_repeat_mutation(self):
        def response(state, body):
            if (
                body["operationName"] == "ResolveReviewThread"
                and body["variables"]["input"]["threadId"] == "PRRT_100"
            ):
                state.resolved_threads.add(100)
                raise GitHubHTTPTransientError("response lost after committed mutation")

        state, result, operations = self.resolve(response)
        self.assertEqual(result.unconfirmed, ())
        self.assertEqual(set(result.resolved), {e.instance for e in state.entries[:3]})
        self.assertEqual(state.resolved_threads, {100, 101, 102})
        mutations = [
            v["input"]["threadId"]
            for op, v in operations
            if op == "ResolveReviewThread"
        ]
        self.assertEqual(mutations, ["PRRT_100", "PRRT_101", "PRRT_102"])
        self.assertEqual([op for op, _ in operations].count("ResolutionReadback"), 1)


class ThreadResolutionRecoveryTests(unittest.TestCase):
    def test_reply_records_authority_before_resolution_and_approval(self):
        host = ResolutionHost()
        outcome = host.reply()
        self.assertTrue(outcome.resolved, outcome)
        self.assertEqual(outcome.approval_status, "approved")
        writes = [body for method, _, body in host.calls if method == "POST"]
        record = next(
            i
            for i, body in enumerate(writes)
            if RESOLUTION_PREFIX in body.get("body", "")
        )
        mutation = next(
            i
            for i, body in enumerate(writes)
            if body.get("operationName") == "ResolveReviewThread"
        )
        self.assertLess(record, mutation)
        self.assertEqual(sum(RESOLUTION_PREFIX in r["body"] for r in host.reviews), 1)
        host.reply()
        self.assertEqual(sum(RESOLUTION_PREFIX in r["body"] for r in host.reviews), 1)

    def test_verify_retries_recorded_decision_after_failed_mutation_without_ai(self):
        host = ResolutionHost()
        host.fail_mutation = True
        reply = host.reply()
        self.assertFalse(reply.resolved)
        self.assertEqual(reply.resolution_status, "unconfirmed")
        host.fail_mutation = False
        outcome = host.verify()
        self.assertTrue(host.resolved)
        self.assertEqual(outcome.status, "approved")

    def test_typed_native_bot_alias_resolves_but_user_and_sibling_aliases_never_do(
        self,
    ):
        for actor, accepted in (
            ({"login": "reviewsensei", "__typename": "Bot"}, True),
            ({"login": "reviewsensei", "__typename": "User"}, False),
            ({"login": "other-app", "__typename": "Bot"}, False),
            ({"login": APP, "__typename": "User"}, False),
        ):
            with self.subTest(actor=actor):
                host = ResolutionHost()
                host.graphql_author = actor
                disposition = FindingDisposition(
                    host.instance,
                    "accept-risk",
                    "acceptable risk",
                    "alice",
                    head_sha=HEAD,
                )
                host.verify((disposition,))
                self.assertEqual(host.resolved, accepted)
                self.assertEqual(
                    any(
                        body and body.get("operationName") == "ResolveReviewThread"
                        for _, _, body in host.calls
                    ),
                    accepted,
                )
                if accepted:
                    queries = [
                        body["query"]
                        for _, _, body in host.calls
                        if body
                        and body.get("operationName")
                        in {"FindingResolutionThreads", "ReviewThreads"}
                    ]
                    self.assertTrue(queries)
                    self.assertTrue(all("__typename" in query for query in queries))

    def test_native_bot_duplicate_suppression_uses_the_same_typed_identity(self):
        from tests import test_github_publication as fixtures
        from tests.fake_github_http import json_response

        current = fixtures.result()
        existing = (
            finding_marker(
                repository_id=1,
                pull_request=2,
                head_sha=HEAD,
                base_sha=BASE,
                result=current,
                blocking=True,
                fingerprint=finding_lifecycle_for_comment(
                    current.comments[0]
                ).fingerprint,
            )
            + "\n\n"
            + finding_instance_marker(current.comments[0])
        )
        outcome, calls = fixtures.ReviewPublisherTests().publish(
            [
                json_response(pr_payload(head_sha=HEAD)),
                json_response([]),
                json_response(pr_payload(head_sha=HEAD)),
                fixtures.graphql_review_threads_response(
                    nodes=(
                        {
                            "isResolved": False,
                            "comments": {
                                "nodes": [
                                    {
                                        "body": existing,
                                        "author": {
                                            "login": "reviewsensei",
                                            "__typename": "Bot",
                                        },
                                    }
                                ]
                            },
                        },
                    )
                ),
                json_response({"id": 5}, 200),
            ]
        )
        self.assertEqual(outcome.status, "published")
        self.assertEqual(fixtures.review_payloads(calls)[0]["comments"], [])

    def test_native_bot_unresolved_blocker_withholds_legacy_unindexed_approval(self):
        host = ResolutionHost()
        host.graphql_author = {"login": "reviewsensei", "__typename": "Bot"}
        host.reviews[0]["body"] = approval_eligibility_marker(host.eligibility)
        outcome = ReviewApprovalFinalizer(http=host).finalize(
            token="token",
            repository="owner/repo",
            pull_request=2,
            head_sha=HEAD,
            app_slug=APP,
            eligibility=host.eligibility,
        )
        self.assertEqual(outcome.status, "approval_withheld")
        self.assertEqual(outcome.diagnostic, "required_fixes_open")
        self.assertFalse(any(r["state"] == "APPROVED" for r in host.reviews))

    def test_ambiguous_expected_native_actor_refuses_instead_of_skipping_blocker(self):
        for discriminator in (
            {},
            {"__typename": None},
            {"__typename": ""},
            {"__typename": False},
            {"__typename": []},
        ):
            with self.subTest(discriminator=discriminator):
                host = ResolutionHost()
                host.graphql_author = {"login": "reviewsensei", **discriminator}
                host.reviews[0]["body"] = approval_eligibility_marker(host.eligibility)
                with self.assertRaises(GitHubPublicationError):
                    ReviewApprovalFinalizer(http=host).finalize(
                        token="token",
                        repository="owner/repo",
                        pull_request=2,
                        head_sha=HEAD,
                        app_slug=APP,
                        eligibility=host.eligibility,
                    )
                self.assertFalse(any(r["state"] == "APPROVED" for r in host.reviews))
                self.assertFalse(host.resolved)

    def test_record_failure_never_mutates(self):
        host = ResolutionHost()
        host.fail_record = True
        outcome = host.reply()
        self.assertFalse(outcome.resolved)
        self.assertFalse(
            any(
                bool(body and body.get("operationName") == "ResolveReviewThread")
                for _, _, body in host.calls
            )
        )

    def test_malformed_authority_preserves_override_and_does_not_mutate(self):
        host = ResolutionHost()
        host.reviews.append(host.review(20, RESOLUTION_PREFIX + "broken -->"))
        disposition = FindingDisposition(
            fingerprint=host.instance,
            action="accept-risk",
            reason="acceptable risk",
            actor="alice",
            head_sha=HEAD,
        )
        outcome = host.verify((disposition,))
        self.assertEqual(outcome.status, "approved")
        self.assertEqual(outcome.thread_resolution, "unconfirmed")
        self.assertFalse(host.resolved)

    def test_resolution_success_survives_approval_failure(self):
        host = ResolutionHost()
        host.fail_approval = True
        outcome = host.reply()
        self.assertTrue(outcome.resolved)
        self.assertEqual(outcome.approval_status, "approval_withheld")
        host.fail_approval = False
        self.assertEqual(host.verify().status, "approved")

    def test_override_resolves_matching_thread_and_preserves_record_on_failure(self):
        for fails in (False, True):
            with self.subTest(fails=fails):
                host = ResolutionHost()
                host.fail_mutation = fails
                disposition = FindingDisposition(
                    fingerprint=host.instance,
                    action="accept-risk",
                    reason="acceptable risk",
                    actor="alice",
                    head_sha=HEAD,
                )
                outcome = host.verify((disposition,))
                self.assertEqual(outcome.status, "approved")
                self.assertEqual(host.resolved, not fails)
                self.assertEqual(
                    outcome.thread_resolution, "unconfirmed" if fails else "resolved"
                )

    def test_head_base_source_and_root_races_prevent_mutation(self):
        changes = (
            lambda h: h.pr["head"].update(sha="c" * 40),
            lambda h: h.pr["base"].update(sha="c" * 40),
            lambda h: h.source.update(body="edited"),
            lambda h: h.root["user"].update(login="alice", type="User"),
        )
        for change in changes:
            with self.subTest(change=change):
                host = ResolutionHost()
                host.before_resolution = change
                outcome = host.reply()
                self.assertFalse(outcome.resolved)
                self.assertFalse(host.resolved)

    def test_lookup_errors_preserve_reply_and_withhold_approval(self):
        for failure in ("malformed", "transport"):
            with self.subTest(failure=failure):
                host = ResolutionHost()
                host.lookup_failure = failure
                outcome = host.reply()
                self.assertFalse(outcome.resolved)
                self.assertEqual(outcome.approval_status, "approval_withheld")

    def test_later_unindexed_receipt_cannot_hide_missing_thread(self):
        host = ResolutionHost()
        host.missing_thread = True
        host.reviews.append(host.review(7, "Later approval/assessment receipt"))
        outcome = ReviewApprovalFinalizer(http=host).finalize(
            token="token",
            repository="owner/repo",
            pull_request=2,
            head_sha=HEAD,
            app_slug=APP,
            eligibility=host.eligibility,
        )
        self.assertEqual(outcome.status, "approval_withheld")

    def test_unrelated_concern_is_never_superseded(self):
        host = ResolutionHost()
        ReviewPublisher(http=host)._supersede_replaced_threads(
            token="token",
            threads=(("PRRT_old", "ab" * 32),),
            confirmed_review_id=5,
            repository="owner/repo",
            repository_id=1,
            pull_request=2,
            head_sha=HEAD,
            base_sha=BASE,
            app_slug=APP,
            eligibility=host.eligibility,
            replacements=((host.comment, host.concern, "body", "right"),),
        )
        self.assertEqual(host.calls, [])

    def test_reassessment_resolves_inline_human_and_manual_closure_does_not_clear_it(
        self,
    ):
        host = ResolutionHost(human=True)
        host.resolved = True
        before = ReviewApprovalFinalizer(http=host).finalize(
            token="token",
            repository="owner/repo",
            pull_request=2,
            head_sha=HEAD,
            app_slug=APP,
            eligibility=host.eligibility,
        )
        self.assertEqual(before.status, "approval_withheld")
        host.resolved = False
        pending = host.eligibility.human_review
        self.assertIsNotNone(pending)
        refreshed = replace(
            host.eligibility,
            human_review=replace(
                pending, resolved=tuple(item.fingerprint for item in pending.findings)
            ),
            facts=replace(
                host.eligibility.facts, has_human_adjudication_findings=False
            ),
        )
        # A reassessment preserves result identity without copying the original index.
        host.reviews.append(host.review(7, approval_eligibility_marker(refreshed)))
        outcome = ReviewApprovalFinalizer(http=host).finalize_recorded_decisions(
            token="token",
            repository="owner/repo",
            pull_request=2,
            head_sha=HEAD,
            app_slug=APP,
            eligibility=refreshed,
        )
        self.assertTrue(host.resolved)
        self.assertEqual(outcome.status, "approved")

    def test_verify_rejects_edited_source_and_removed_authority(self):
        for remove in (False, True):
            with self.subTest(remove=remove):
                host = ResolutionHost()
                host.fail_mutation = True
                host.reply()
                host.fail_mutation = False
                if remove:
                    host.reviews = [
                        item
                        for item in host.reviews
                        if RESOLUTION_PREFIX not in item["body"]
                    ]
                else:
                    host.source["body"] = "Edited since the recorded decision"
                result = host.verify()
                self.assertFalse(host.resolved)
                self.assertEqual(result.status, "approval_withheld")

    def test_body_override_has_no_thread_mutation(self):
        host = ResolutionHost()
        host.index = finding_index_marker(
            repository_id=1,
            pull_request=2,
            head_sha=HEAD,
            base_sha=BASE,
            result_digest=host.eligibility.result_digest,
            entries=(
                IndexedFinding(
                    instance=host.instance,
                    lifecycle=host.concern,
                    blocking=True,
                    needs_human=False,
                    body_placed=True,
                ),
            ),
        )
        host.eligibility = replace(
            host.eligibility,
            facts=replace(host.eligibility.facts, has_blocking_findings=True),
        )
        host.reviews[0]["body"] = (
            approval_eligibility_marker(host.eligibility) + "\n" + host.index
        )
        host.missing_thread = True
        disposition = FindingDisposition(
            fingerprint=host.instance,
            action="accept-risk",
            reason="acceptable risk",
            actor="alice",
            head_sha=HEAD,
        )
        result = host.verify((disposition,))
        self.assertEqual(result.status, "approved")
        self.assertFalse(
            any(
                body and body.get("operationName") == "ResolveReviewThread"
                for _, _, body in host.calls
            )
        )

    def test_new_result_without_matching_inventory_withholds(self):
        host = ResolutionHost()
        changed = replace(host.eligibility, result_digest="f" * 64)
        host.reviews.append(host.review(7, approval_eligibility_marker(changed)))
        result = ReviewApprovalFinalizer(http=host).finalize(
            token="token",
            repository="owner/repo",
            pull_request=2,
            head_sha=HEAD,
            app_slug=APP,
            eligibility=changed,
        )
        self.assertEqual(result.status, "approval_withheld")

    def test_lost_mutation_response_is_confirmed_by_readback(self):
        host = ResolutionHost()
        respond = host.respond

        def ambiguous(method, path, body):
            if path == "/graphql" and body["operationName"] == "ResolveReviewThread":
                host.resolved = True
                raise GitHubHTTPTransientError("response lost after mutation")
            return respond(method, path, body)

        host.respond = ambiguous
        result = host.reply()
        self.assertTrue(result.resolved)
        self.assertEqual(
            sum(
                bool(body and body.get("operationName") == "ResolveReviewThread")
                for _, _, body in host.calls
            ),
            1,
        )

    def test_resolution_pagination_finishes_before_any_mutation(self):
        host = ResolutionHost()
        respond = host.respond
        pages = []

        def paginated(method, path, body):
            status, payload = respond(method, path, body)
            if (
                path == "/graphql"
                and body["operationName"] == "FindingResolutionThreads"
            ):
                pages.append(body["variables"]["after"])
                payload["data"]["repository"]["pullRequest"]["reviewThreads"][
                    "pageInfo"
                ] = {"hasNextPage": True, "endCursor": str(len(pages))}
            return status, payload

        host.respond = paginated
        result = host.reply()
        self.assertEqual(len(pages), 10)
        self.assertFalse(result.resolved)
        self.assertFalse(
            any(
                body and body.get("operationName") == "ResolveReviewThread"
                for _, _, body in host.calls
            )
        )

    def test_supersession_needs_confirmed_replacement_and_current_base(self):
        for missing, stale in ((False, False), (True, False), (False, True)):
            with self.subTest(missing=missing, stale=stale):
                host = ResolutionHost()
                old = copy.deepcopy(host.root)
                old["id"] = 30
                old["body"] = old["body"].replace("head=" + HEAD, "head=" + "c" * 40)
                respond = host.respond
                mutations = []

                def supersession(method, path, body):
                    if path.endswith("/pulls/comments/30"):
                        return 200, old
                    if (
                        path == "/graphql"
                        and body["operationName"] == "ResolveReviewThread"
                    ):
                        mutations.append(body["variables"]["input"]["threadId"])
                        return 200, {
                            "data": {
                                "resolveReviewThread": {
                                    "thread": {"id": "PRRT_old", "isResolved": True}
                                }
                            }
                        }
                    status, payload = respond(method, path, body)
                    if path == "/graphql":
                        nodes = payload["data"]["repository"]["pullRequest"][
                            "reviewThreads"
                        ]["nodes"]
                        if missing:
                            nodes.clear()
                        nodes.append(
                            {
                                "id": "PRRT_old",
                                "isResolved": False,
                                "comments": {
                                    "nodes": [
                                        {
                                            "databaseId": 30,
                                            "body": old["body"],
                                            "author": {"login": APP},
                                        }
                                    ]
                                },
                            }
                        )
                    return status, payload

                host.respond = supersession
                if stale:
                    host.pr["base"]["sha"] = "d" * 40
                ReviewPublisher(http=host)._supersede_replaced_threads(
                    token="token",
                    threads=(("PRRT_old", host.concern),),
                    confirmed_review_id=5,
                    repository="owner/repo",
                    repository_id=1,
                    pull_request=2,
                    head_sha=HEAD,
                    base_sha=BASE,
                    app_slug=APP,
                    eligibility=host.eligibility,
                    replacements=((host.comment, host.concern, "body", "right"),),
                )
                self.assertEqual(mutations, [] if missing or stale else ["PRRT_old"])
