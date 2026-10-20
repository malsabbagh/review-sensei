"""Bounded v1 default publication retains complete identities and explanations."""

import base64
import json
import unittest
from dataclasses import replace
from hashlib import sha256
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse

from review_sensei_github import (
    GitHubHttp,
    GitHubHTTPPaginationLimitError,
    GitHubHTTPResponseTooLargeError,
    GitHubHTTPTransientError,
    GitHubPublicationError,
    GitHubPublicationTransientError,
    ReviewPublisher,
)
from review_sensei_github.publication import (
    IndexedFinding,
    PublicationLimitError,
    ReviewApprovalFinalizer,
    approval_eligibility_from_body,
    finding_index_marker,
    parse_finding_index,
    review_result_digest,
)

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.context import finding_lifecycle_for_comment
from review_sensei.convergence import BlockerCandidate, ReviewConvergencePolicy
from review_sensei.errors import ReviewInputError
from review_sensei.human_assessment import finding_instance_fingerprint
from review_sensei.models import ReviewComment, ReviewResult, ReviewTransaction
from tests.fake_github_http import json_response
from tests.test_human_assessment import APP, BASE, DIFF, HEAD, State


def comments(count, *, body_placed=False, detail=0):
    return tuple(
        ReviewComment(
            path="src/app.py",
            line=None if body_placed else 1,
            side="FILE" if body_placed else "RIGHT",
            body=f"Distinct finding {i:03}: preserve its original explanation."
            + "x" * detail,
            blocking=True,
            symbol=f"case_{i}",
            defect_kind="fixture",
            evidence_id=f"evidence-{i}",
        )
        for i in range(count)
    )


def publish(state, findings, **overrides):
    arguments = {
        "token": "synthetic-review",
        "check_token": "synthetic-check",
        "repository": "owner/repo",
        "repository_id": 1,
        "pull_request": 1,
        "head_sha": HEAD,
        "base_branch": "main",
        "base_sha": BASE,
        "app_slug": APP,
        "convergence_policy": ReviewConvergencePolicy(enforcement="publication"),
        "result": ReviewResult(
            summary="Complete bounded inventory.",
            comments=findings,
            provider="fixture",
            review_status="complete",
        ),
        "diff": "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n"
        + DIFF,
        "complete_inventory": True,
        "auto_approve": False,
    }
    arguments.update(overrides)
    return ReviewPublisher(http=state.http).publish(**arguments)


def inline_state(*, response_metadata_bytes=0):
    state = State()
    state.reviews = []
    state.inline_comments = None
    state.readback_pages = []
    ordinary = state.http.opener

    def open_request(request, timeout):
        parsed = urlparse(request.full_url)
        if (
            request.method == "GET"
            and "/reviews/" in parsed.path
            and parsed.path.endswith("/comments")
        ):
            state.calls.append(("GET", parsed.path, None))
            review_id = state.reviews[0]["id"]
            observations = state.inline_comments
            if observations is None:
                posted = next(
                    body
                    for method, path, body in state.calls
                    if method == "POST"
                    and path.endswith("/reviews")
                    and "comments" in body
                )
                observations = [
                    {
                        "id": 1000 + i,
                        "pull_request_review_id": review_id,
                        "user": {"login": APP, "type": "Bot"},
                        "body": item["body"],
                        "diff_hunk": "x" * response_metadata_bytes,
                    }
                    for i, item in enumerate(posted["comments"])
                ]
            query = parse_qs(parsed.query)
            size, page = int(query["per_page"][0]), int(query["page"][0])
            state.readback_pages.append((size, page))
            return json_response(observations[(page - 1) * size : page * size])
        return ordinary(request, timeout)

    state.http.opener = open_request
    return state


class CompleteInventoryPublicationTests(unittest.TestCase):
    def test_shared_publication_transport_charges_each_physical_review_read_once(self):
        state = State()
        budget = EvidenceReadBudget()

        class SharedTransport(GitHubHttp):
            def __init__(self):
                self.publication_budget = budget
                self.api_url = state.http.api_url

            def request(self, method, path, *, token, body=None, timeout_seconds=None):
                remaining = budget.consume()
                return state.http.request(
                    method,
                    path,
                    token=token,
                    body=body,
                    timeout_seconds=min(remaining, timeout_seconds)
                    if timeout_seconds is not None
                    else remaining,
                )

        finalizer = ReviewApprovalFinalizer(
            http=SharedTransport(), evidence_budget=budget
        )
        self.assertIsNotNone(
            finalizer.load_eligibility(
                token="synthetic",
                repository="owner/repo",
                pull_request=1,
                head_sha=HEAD,
                app_slug=APP,
                require_valid=True,
            )
        )
        physical = sum(
            path.endswith("/reviews")
            for method, path, _ in state.calls
            if method == "GET"
        )
        self.assertEqual(physical, 1)
        self.assertEqual(budget.calls, physical)

    def assert_no_mutations(self, state):
        self.assertFalse(
            [
                (method, path)
                for method, path, _ in state.calls
                if method != "GET" and path != "/graphql"
            ]
        )

    def assert_failed_check_only(self, state):
        mutations = [
            (method, path)
            for method, path, body in state.calls
            if method != "GET"
            and (
                path != "/graphql" or body.get("operationName") == "ResolveReviewThread"
            )
        ]
        self.assertTrue(mutations)
        self.assertTrue(all("/check-runs" in path for _, path in mutations))
        self.assertEqual(state.check_runs[-1]["conclusion"], "action_required")

    def test_77_findings_publish_all_full_text_and_exact_identities(self):
        findings = comments(77)
        state = State()
        state.reviews = []
        outcome = publish(state, findings)
        self.assertEqual(outcome.status, "published")
        writes = [
            body
            for method, path, body in state.calls
            if method == "POST" and path.endswith("/pulls/1/reviews")
        ]
        self.assertEqual(len(writes), 1)
        review = writes[0]
        self.assertNotEqual(review["event"], "APPROVE")
        index = parse_finding_index(
            review["body"], repository_id=1, pull_request=1, head_sha=HEAD
        )
        expected = {
            finding_instance_fingerprint(item): finding_lifecycle_for_comment(
                item
            ).fingerprint
            for item in findings
        }
        self.assertEqual(
            {item.instance: item.lifecycle for item in index.entries}, expected
        )
        text = review["body"] + "\n".join(item["body"] for item in review["comments"])
        for finding in findings:
            self.assertIn(finding.body, text)
        self.assertTrue(
            all(not item.blocking and not item.needs_human for item in index.entries)
        )
        self.assertNotIn("omitted for size", text)

    def test_complete_writer_admits_64_65_100_and_compact_250_with_full_text(self):
        for count in (64, 65, 100, 250):
            with self.subTest(count=count):
                findings = tuple(
                    replace(item, body=f"F{i:03}.") if count == 250 else item
                    for i, item in enumerate(comments(count, body_placed=True))
                )
                state = State()
                state.reviews = []
                self.assertEqual(publish(state, findings).status, "published")
                review = state.reviews[0]
                index = parse_finding_index(
                    review["body"], repository_id=1, pull_request=1, head_sha=HEAD
                )
                self.assertEqual(
                    {entry.instance for entry in index.entries},
                    {finding_instance_fingerprint(item) for item in findings},
                )
                self.assertLessEqual(len(review["body"].encode()), 65_536)
                for finding in findings:
                    self.assertIn(finding.body, review["body"])
                self.assertNotIn("omitted for size", review["body"])

    def test_legacy_emission_gate_keeps_64_and_refuses_65_with_count_diagnostic(self):
        state = State()
        state.reviews = []
        self.assertEqual(
            publish(state, comments(64), complete_inventory=False).status, "published"
        )
        state = State()
        state.reviews = []
        with self.assertRaises(PublicationLimitError) as raised:
            publish(state, comments(65), complete_inventory=False)
        self.assertEqual(raised.exception.check, "finding-index-count")
        self.assertFalse(state.reviews)

    def test_complete_body_overflow_refuses_optional_text_without_any_mutation(self):
        state = State()
        state.reviews = []
        findings = comments(6, body_placed=True, detail=12_000)
        with self.assertRaises(PublicationLimitError) as raised:
            publish(state, findings)
        self.assertIn(
            raised.exception.check, {"published-summary", "framed-review-body"}
        )
        self.assert_no_mutations(state)
        self.assertFalse(state.reviews)

    def test_maximum_individual_body_is_preserved_and_oversized_input_refuses(self):
        state = State()
        state.reviews = []
        maximum = replace(comments(1)[0], body="Full body: " + "x" * (16_384 - 11))
        self.assertEqual(len(maximum.body.encode()), 16_384)
        self.assertEqual(publish(state, (maximum,)).status, "published")
        self.assertIn(maximum.body, state.reviews[0]["body"])
        state = State()
        with self.assertRaises(ReviewInputError):
            publish(state, (replace(maximum, body=maximum.body + "x"),))
        self.assert_no_mutations(state)

    def test_malformed_persisted_index_refuses_replay_before_mutation(self):
        state = State()
        state.reviews = []
        findings = comments(77)
        self.assertEqual(publish(state, findings).status, "published")
        state.reviews[0]["body"] += "\n<!-- reviewsensei:finding-index:v1 invalid -->"
        state.calls.clear()
        with self.assertRaises((GitHubPublicationError, ReviewInputError)):
            publish(state, findings)
        self.assert_failed_check_only(state)

    def test_legacy_review_missing_optional_prose_is_not_upgraded_by_replay(self):
        state = State()
        state.reviews = []
        findings = comments(2, body_placed=True)
        self.assertEqual(
            publish(state, findings, complete_inventory=False).status, "published"
        )
        state.reviews[0]["body"] = state.reviews[0]["body"].replace(
            findings[0].body, "Explanation omitted for size."
        )
        state.calls.clear()
        with self.assertRaisesRegex(GitHubPublicationError, "complete.*prose"):
            publish(state, findings)
        self.assert_failed_check_only(state)

    def test_complete_body_publication_replays_without_a_second_review(self):
        state = State()
        state.reviews = []
        findings = comments(77)
        self.assertEqual(publish(state, findings).status, "published")
        state.calls.clear()
        self.assertEqual(publish(state, findings).status, "already_published")
        self.assertFalse(
            [
                path
                for method, path, _ in state.calls
                if method == "POST" and path.endswith("/reviews")
            ]
        )

    def test_complete_inline_prose_replays_from_owned_review_specific_comments(self):
        state = inline_state()
        findings = comments(77, detail=200)
        options = {
            "convergence_policy": ReviewConvergencePolicy(mode="legacy"),
            "allow_retired_legacy_policy": True,
        }
        self.assertEqual(publish(state, findings, **options).status, "published")
        review_write = next(
            body
            for method, path, body in state.calls
            if method == "POST" and path.endswith("/reviews")
        )
        self.assertEqual(len(review_write["comments"]), 77)
        for item, finding in zip(review_write["comments"], findings, strict=True):
            self.assertIn(finding.body, item["body"])
        before = len(state.reviews)
        self.assertEqual(
            publish(state, findings, **options).status, "already_published"
        )
        self.assertEqual(len(state.reviews), before)
        self.assertTrue(
            any(
                "/reviews/" in path and path.endswith("/comments")
                for _, path, _ in state.calls
            )
        )

    def test_large_inline_metadata_reconciles_and_replays_with_bounded_pages(self):
        for count in (100, 250):
            with self.subTest(count=count):
                state = inline_state(response_metadata_bytes=6000)
                findings = comments(count, detail=200)
                options = {
                    "convergence_policy": ReviewConvergencePolicy(mode="legacy"),
                    "allow_retired_legacy_policy": True,
                }
                self.assertEqual(
                    publish(state, findings, **options).status, "published"
                )
                self.assertEqual(
                    publish(state, findings, **options).status, "already_published"
                )
                self.assertTrue(any(size < 100 for size, _ in state.readback_pages))
                writes = [
                    body
                    for method, path, body in state.calls
                    if method == "POST" and path.endswith("/reviews")
                ]
                self.assertEqual(len(writes), 1)
                self.assertEqual(len(writes[0]["comments"]), count)
                self.assertNotEqual(writes[0]["event"], "APPROVE")

    def test_inline_readback_size_and_request_limits_still_refuse_without_writes(self):
        for count, metadata, cause in (
            (1, 600_000, GitHubHTTPResponseTooLargeError),
            (250, 20_000, GitHubHTTPPaginationLimitError),
        ):
            with self.subTest(count=count, metadata=metadata):
                state = inline_state(response_metadata_bytes=metadata)
                findings = comments(count, detail=200)
                options = {
                    "convergence_policy": ReviewConvergencePolicy(mode="legacy"),
                    "allow_retired_legacy_policy": True,
                }
                self.assertEqual(
                    publish(state, findings, **options).status, "published"
                )
                before = [call for call in state.calls if call[0] != "GET"]
                with self.assertRaises(GitHubPublicationError) as failure:
                    publish(state, findings, **options)
                self.assertIsInstance(failure.exception.__cause__, cause)
                self.assertEqual(
                    [call for call in state.calls if call[0] != "GET"], before
                )
                self.assertLessEqual(len(state.readback_pages), 10)

    def test_missing_or_foreign_inline_prose_refuses_before_check_or_approval(self):
        for foreign in (False, True):
            with self.subTest(foreign=foreign):
                state = inline_state()
                findings = comments(1)
                options = {
                    "convergence_policy": ReviewConvergencePolicy(mode="legacy"),
                    "allow_retired_legacy_policy": True,
                }
                self.assertEqual(
                    publish(state, findings, **options).status, "published"
                )
                posted = next(
                    body
                    for method, path, body in state.calls
                    if method == "POST" and path.endswith("/reviews")
                )
                state.inline_comments = [
                    {
                        "id": 1000,
                        "pull_request_review_id": state.reviews[0]["id"],
                        "user": {
                            "login": "mallory" if foreign else APP,
                            "type": "User" if foreign else "Bot",
                        },
                        "body": posted["comments"][0]["body"]
                        if foreign
                        else "Missing complete explanation.",
                    }
                ]
                state.calls.clear()
                # Retain the prior POST payload in this synthetic transport's
                # response closure while independently inspecting new calls.
                # Explicit observations mean it need not infer remote content.
                with self.assertRaises(GitHubPublicationError):
                    publish(state, findings, **options)
                self.assert_failed_check_only(state)

    def test_transient_complete_inline_readback_keeps_transport_refusal(self):
        state = inline_state()
        findings = comments(1)
        options = {
            "convergence_policy": ReviewConvergencePolicy(mode="legacy"),
            "allow_retired_legacy_policy": True,
        }
        self.assertEqual(publish(state, findings, **options).status, "published")
        original = state.http.opener

        def unavailable(request, timeout):
            path = urlparse(request.full_url).path
            if request.method == "GET" and "/reviews/" in path:
                raise URLError("synthetic unavailable inline readback")
            return original(request, timeout)

        state.http.opener = unavailable
        state.calls.clear()
        with self.assertRaises(GitHubPublicationTransientError) as failure:
            publish(state, findings, **options)
        self.assertIsInstance(failure.exception.__cause__, GitHubHTTPTransientError)
        self.assert_no_mutations(state)

    def test_human_and_path_future_bounds_remain_closed_before_mutation(self):
        cases = (
            tuple(
                replace(item, path=f"src/{i}.py", needs_human=True)
                for i, item in enumerate(comments(65))
            ),
            tuple(
                replace(
                    item,
                    needs_human=True,
                    body="".join(
                        sha256(f"{i}:{j}".encode()).hexdigest() for j in range(16)
                    ),
                )
                for i, item in enumerate(comments(250))
            ),
        )
        for findings in cases:
            with self.subTest(count=len(findings)):
                state = State()
                with self.assertRaises(ReviewInputError):
                    publish(
                        state,
                        findings,
                        blocker_candidates=tuple(
                            BlockerCandidate(
                                needs_human=True,
                                path=item.path,
                                line=item.line,
                                side=item.side,
                            )
                            for item in findings
                        ),
                    )
                self.assert_no_mutations(state)

    def test_human_inventory_keeps_77_pending_decisions_and_full_bodies(self):
        state = State()
        state.reviews = []
        findings = tuple(
            replace(item, needs_human=True) for item in comments(77, body_placed=True)
        )
        self.assertEqual(
            publish(
                state,
                findings,
                blocker_candidates=tuple(
                    BlockerCandidate(
                        needs_human=True, path=item.path, line=item.line, side=item.side
                    )
                    for item in findings
                ),
            ).status,
            "published",
        )
        eligibility = approval_eligibility_from_body(state.reviews[0]["body"])
        self.assertEqual(len(eligibility.human_review.pending), 77)
        self.assertTrue(eligibility.facts.has_human_adjudication_findings)
        for item in findings:
            self.assertIn(item.body, state.reviews[0]["body"])
        self.assertFalse(
            any(review.get("state") == "APPROVED" for review in state.reviews)
        )

    def test_duplicate_instances_coalesce_and_conflicting_classifications_refuse(self):
        state = State()
        state.reviews = []
        finding = comments(1)[0]
        self.assertEqual(publish(state, (finding, finding)).status, "published")
        index = parse_finding_index(
            state.reviews[0]["body"], repository_id=1, pull_request=1, head_sha=HEAD
        )
        self.assertEqual(len(index.entries), 1)
        state = State()
        with self.assertRaises((GitHubPublicationError, ReviewInputError)):
            publish(state, (finding, replace(finding, blocking=False)))
        self.assert_no_mutations(state)

    def test_transactional_replay_identity_survives_phase_and_generation_rebind(self):
        findings = comments(77)
        result = ReviewResult(
            summary="Bound transaction.",
            comments=findings,
            provider="fixture",
            review_status="complete",
        )
        transaction = ReviewTransaction.create(
            repository="owner/repo",
            pull_request=1,
            base_sha=BASE,
            head_sha=HEAD,
            policy_digest="c" * 64,
            configuration_digest="d" * 64,
            evidence_digest="e" * 64,
            reservation_id="f" * 64,
            generation=3,
        ).with_result(result.content_digest())
        bound = replace(result, transaction=transaction)
        recovered = replace(
            result,
            transaction=replace(transaction, generation=9, phase="publication_failed"),
        )
        self.assertEqual(review_result_digest(bound), review_result_digest(recovered))
        state = State()
        state.reviews = []
        self.assertEqual(publish(state, findings, result=bound).status, "published")
        state.calls.clear()
        self.assertEqual(
            publish(state, findings, result=recovered).status, "already_published"
        )
        self.assertFalse(
            [
                path
                for method, path, _ in state.calls
                if method == "POST" and path.endswith("/reviews")
            ]
        )

    def test_transactional_identity_does_not_accept_unrelated_attempt_or_content(self):
        findings = comments(1)
        result = ReviewResult(
            summary="Bound transaction.",
            comments=findings,
            provider="fixture",
            review_status="complete",
        )

        def bind(content, reservation):
            return replace(
                content,
                transaction=ReviewTransaction.create(
                    repository="owner/repo",
                    pull_request=1,
                    base_sha=BASE,
                    head_sha=HEAD,
                    policy_digest="c" * 64,
                    configuration_digest="d" * 64,
                    evidence_digest="e" * 64,
                    reservation_id=reservation,
                    generation=3,
                ).with_result(content.content_digest()),
            )

        bound = bind(result, "f" * 64)
        unrelated = bind(result, "a" * 64)
        changed = bind(replace(result, summary="Different analysis."), "f" * 64)
        self.assertNotEqual(
            review_result_digest(bound), review_result_digest(unrelated)
        )
        self.assertNotEqual(review_result_digest(bound), review_result_digest(changed))
        state = State()
        state.reviews = []
        self.assertEqual(publish(state, findings, result=bound).status, "published")
        state.calls.clear()
        with self.assertRaisesRegex(
            GitHubPublicationError, "does not prove complete publication"
        ):
            publish(state, findings, result=unrelated)
        self.assert_no_mutations(state)

    def test_unbound_result_keeps_historical_publication_digest(self):
        result = ReviewResult(
            summary="Legacy digest.",
            comments=(),
            provider="fixture",
            review_status="complete",
        )
        historical = sha256(
            json.dumps(
                result.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        self.assertEqual(review_result_digest(result), historical)

    def test_actual_lost_review_post_ack_reconciles_full_body_without_duplicate(self):
        state = State()
        state.reviews = []
        original = state.http.opener
        lost = False

        def transport(request, timeout):
            nonlocal lost
            response = original(request, timeout)
            if (
                request.method == "POST"
                and urlparse(request.full_url).path.endswith("/reviews")
                and not lost
            ):
                lost = True
                raise URLError("synthetic lost review response")
            return response

        state.http.opener = transport
        findings = comments(77)
        self.assertEqual(publish(state, findings).status, "already_published")
        self.assertTrue(lost)
        self.assertEqual(len(state.reviews), 1)
        state.calls.clear()
        self.assertEqual(publish(state, findings).status, "already_published")
        self.assertEqual(len(state.reviews), 1)
        self.assertFalse(
            [
                path
                for method, path, _ in state.calls
                if method == "POST" and path.endswith("/reviews")
            ]
        )

    def test_ambiguous_post_cannot_reconcile_an_unrelated_same_head_review(self):
        other = State()
        other.reviews = []
        self.assertEqual(publish(other, comments(1)).status, "published")
        unrelated = other.reviews[0]
        state = State()
        state.reviews = []
        original = state.http.opener

        def transport(request, timeout):
            if request.method == "POST" and urlparse(request.full_url).path.endswith(
                "/reviews"
            ):
                state.reviews.append(unrelated)
                raise URLError("synthetic rejected review with lost response")
            return original(request, timeout)

        state.http.opener = transport
        with self.assertRaises(GitHubPublicationError):
            publish(state, comments(77))
        self.assertEqual(state.reviews, [unrelated])

    def test_index_writer_rejects_invalid_width_case_flags_and_duplicates(self):
        valid = IndexedFinding("a" * 64, "b" * 64, False, True)
        for entries in (
            (replace(valid, instance="a" * 62),),
            (replace(valid, lifecycle="b" * 66),),
            (replace(valid, instance="A" * 64),),
            (replace(valid, lifecycle=None),),
            (replace(valid, instance="not a digest"),),
            (replace(valid, blocking=1),),
            (replace(valid, needs_human="true"),),
            (replace(valid, body_placed=0),),
            (replace(valid, overridable=None),),
            (valid, valid),
        ):
            with self.subTest(entries=entries):
                with self.assertRaises(GitHubPublicationError):
                    finding_index_marker(
                        repository_id=1,
                        pull_request=1,
                        head_sha=HEAD,
                        base_sha=BASE,
                        result_digest="c" * 64,
                        entries=entries,
                        complete_inventory=True,
                    )

    def test_reader_refuses_unknown_flags_partial_records_and_duplicate_markers(self):
        valid = finding_index_marker(
            repository_id=1,
            pull_request=1,
            head_sha=HEAD,
            base_sha=BASE,
            result_digest="c" * 64,
            entries=(IndexedFinding("a" * 64, "b" * 64, False, True),),
        )
        prefix = valid.split("entries=", 1)[0] + "entries="
        raw = bytes.fromhex("a" * 64 + "b" * 64) + bytes([2])
        for body in (
            prefix + base64.urlsafe_b64encode(raw[:-1]).decode().rstrip("=") + " -->",
            prefix
            + base64.urlsafe_b64encode(raw[:-1] + bytes([16])).decode().rstrip("=")
            + " -->",
            prefix + base64.urlsafe_b64encode(raw + raw).decode().rstrip("=") + " -->",
            valid + "\n" + valid,
        ):
            with self.subTest(body=body):
                with self.assertRaises(ReviewInputError):
                    parse_finding_index(
                        body, repository_id=1, pull_request=1, head_sha=HEAD
                    )


if __name__ == "__main__":
    unittest.main()
