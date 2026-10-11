"""Public adapter journeys against a mutable synthetic GitHub authority.

Each request/confirmation/evaluation is a separate explicit host invocation
(humans can reply days later). Within an invocation the original budget object
is never replaced. No inference, broker or whole hosted 64/60 claim is made.
"""

import copy
import unittest
from dataclasses import replace

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.coverage import CoverageManifest, FileCoverage
from review_sensei.errors import ReviewInputError
from review_sensei.hosting.github.approval import (
    ReviewApprovalEligibility,
    approval_facts_from_result,
)
from review_sensei.hosting.github.human_file_review import (
    HumanFileReviewPolicy,
    HumanFileReviewPublisher,
)
from review_sensei.hosting.github.publication import (
    approval_eligibility_marker,
    review_result_digest,
)
from review_sensei.models import ReviewComment, ReviewResult

BASE, HEAD = "a" * 40, "b" * 40
API = "https://api.github.test"
REPO = "/repos/owner/repo"
BOT = {"id": 500, "login": "reviewsensei[bot]", "type": "Bot"}
ALICE = {"id": 123, "login": "alice", "type": "User"}


class State:
    def __init__(self):
        self.base, self.head = BASE, HEAD
        self.old = {"photo.png": ("c" * 40, "100644")}
        self.new = {
            "photo.png": ("d" * 40, "100644"),
            "movie.mp4": ("e" * 40, "100644"),
        }
        self.files = [
            {"filename": "photo.png", "status": "modified"},
            {"filename": "movie.mp4", "status": "added"},
            {"filename": "app.py", "status": "modified"},
        ]
        coverage = CoverageManifest(
            files=(
                FileCoverage("photo.png", "unsupported", "binary"),
                FileCoverage("movie.mp4", "unsupported", "binary"),
                FileCoverage("app.py", "reviewed"),
            ),
            enumerated_paths=("photo.png", "movie.mp4", "app.py"),
        )
        self.result = ReviewResult(
            "Complete text analysis; binary files need human review.",
            (),
            "fixture",
            review_status="partial",
            coverage=coverage,
            coverage_only_partial=True,
        )
        self.root = {"id": 10, "user": BOT, "commit_id": HEAD, "state": "COMMENTED"}
        self.set_result(self.result)
        self.reviews = [self.root]
        self.comments = {}
        self.next_id = 100
        self.permission = "write"
        self.calls = []
        self.hook = None

    def set_result(self, result, **facts):
        self.result = result
        frozen = approval_facts_from_result(result, enabled=True, app_authored=False)
        frozen = replace(frozen, **facts)
        eligibility = ReviewApprovalEligibility(
            HEAD, review_result_digest(result), frozen
        )
        self.root["body"] = approval_eligibility_marker(eligibility)

    def source(self, request, selected=None, identifier=20):
        selected = selected or tuple(x.file_id for x in request.files)
        self.comments[identifier] = {
            "id": identifier,
            "user": ALICE,
            "author_association": "COLLABORATOR",
            "issue_url": f"{API}{REPO}/issues/2",
            "updated_at": "2026-10-10T01:00:00Z",
            "body": f"@sensei media-reviewed {request.digest} " + " ".join(selected),
        }

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs.get("timeout_seconds")))
        if self.hook:
            self.hook(method, path)
        path = path.removeprefix(REPO)
        if method == "POST" and path == "/pulls/2/reviews":
            payload = kwargs["body"]
            self.next_id += 1
            value = {
                "id": self.next_id,
                "user": BOT,
                "body": payload["body"],
                "commit_id": payload["commit_id"],
                "state": "APPROVED"
                if payload.get("event") == "APPROVE"
                else "COMMENTED",
            }
            self.reviews.append(value)
            return 201, copy.deepcopy(value)
        if method == "POST":
            self.next_id += 1
            value = {
                "id": self.next_id,
                "user": BOT,
                "body": kwargs["body"]["body"],
                "issue_url": f"{API}{REPO}/issues/2",
            }
            self.comments[self.next_id] = value
            return 201, copy.deepcopy(value)
        if path == "":
            value = {"id": 1, "full_name": "owner/repo"}
        elif path == "/pulls/2":
            value = {
                "state": "open",
                "number": 2,
                "base": {"sha": self.base},
                "head": {"sha": self.head},
            }
        elif path == "/pulls/2/reviews/10":
            value = self.root
        elif path.startswith("/pulls/2/reviews/"):
            review_id = int(path.rsplit("/", 1)[1])
            value = next(item for item in self.reviews if item["id"] == review_id)
        elif path.startswith("/pulls/2/reviews?"):
            value = self.reviews
        elif path.startswith("/pulls/2/files?"):
            value = self.files
        elif path.startswith("/issues/2/comments?"):
            value = list(self.comments.values())
        elif path.startswith("/issues/comments/"):
            value = self.comments.get(int(path.rsplit("/", 1)[1]))
            if value is None:
                return 404, {}
        elif path == "/collaborators/alice/permission":
            value = {"permission": self.permission, "user": ALICE}
        elif path.startswith("/git/commits/"):
            commit = path.rsplit("/", 1)[1]
            value = {
                "sha": commit,
                "tree": {"sha": "1" * 40 if commit == BASE else "2" * 40},
            }
        elif path.startswith("/git/trees/"):
            tree = path.rsplit("/", 1)[1]
            blobs = self.old if tree == "1" * 40 else self.new
            value = {
                "sha": tree,
                "truncated": False,
                "tree": [
                    {"path": path, "type": "blob", "sha": sha, "mode": mode}
                    for path, (sha, mode) in blobs.items()
                ],
            }
        else:
            raise AssertionError(path)
        return 200, copy.deepcopy(value)


class GitHubHumanFileReviewTests(unittest.TestCase):
    def setUp(self):
        self.state = State()

    def publisher(self, enabled=True):
        self.budget = EvidenceReadBudget(clock=lambda: 100.0)
        return HumanFileReviewPublisher(
            http=self.state,
            api_url=API,
            repository="owner/repo",
            repository_id=1,
            pull_request=2,
            app_login=BOT["login"],
            app_user_id=BOT["id"],
            token="synthetic",
            before_request=self.budget.consume,
            policy=HumanFileReviewPolicy(enabled),
        )

    def request_review(self):
        identifier, request = self.publisher().request_review(
            review_id=10, result=self.state.result
        )
        self.assertEqual(self.budget.calls, len(self.state.calls))
        return identifier, request

    def confirmed(self):
        identifier, request = self.request_review()
        self.state.source(request)
        receipt = self.publisher().confirm(
            request_comment_id=identifier,
            source_comment_id=20,
            result=self.state.result,
        )
        return identifier, request, receipt

    def evaluate(self, identifier, **kwargs):
        return self.publisher().evaluate(
            request_comment_id=identifier,
            result=self.state.result,
            has_open_review_threads=False,
            **kwargs,
        )

    def posts(self):
        return sum(method == "POST" for method, _, _ in self.state.calls)

    def test_partial_confirmation_and_fresh_final_evaluation_keep_ai_coverage(self):
        identifier, request = self.request_review()
        self.state.source(request, (request.files[0].file_id,))
        start = len(self.state.calls)
        self.publisher().confirm(
            request_comment_id=identifier,
            source_comment_id=20,
            result=self.state.result,
        )
        self.assertEqual(self.budget.calls, len(self.state.calls) - start)
        assessment = self.publisher().evaluate(
            request_comment_id=identifier,
            result=self.state.result,
            has_open_review_threads=False,
        )
        self.assertFalse(assessment.approval.approved)
        self.assertEqual(len(assessment.pending_ids), 1)
        self.assertIn("not AI-reviewed", assessment.render())
        self.state.source(request, (request.files[1].file_id,), identifier=21)
        self.publisher().confirm(
            request_comment_id=identifier,
            source_comment_id=21,
            result=self.state.result,
        )
        assessment = self.publisher().evaluate(
            request_comment_id=identifier,
            result=self.state.result,
            has_open_review_threads=False,
        )
        self.assertTrue(assessment.approval.approved)
        self.assertEqual(assessment.ai_reviewed_paths, ("app.py",))
        self.assertEqual(len(assessment.human_reviewed_ids), 2)
        self.assertEqual(self.state.result.review_status, "partial")
        self.assertFalse(self.state.result.coverage.fully_reviewed)
        self.assertTrue(
            all(
                method != "POST" or "/issues/2/comments" in path
                for method, path, _ in self.state.calls
            )
        )

    def test_explicit_verify_posts_exact_head_approve_and_keeps_ai_partial(self):
        identifier, request = self.request_review()
        self.state.source(request)
        self.publisher().confirm(
            request_comment_id=identifier,
            source_comment_id=20,
            result=self.state.result,
        )
        assessment = self.publisher().publish_mixed_approval(
            request_comment_id=identifier,
            result=self.state.result,
            has_open_review_threads=False,
        )
        self.assertTrue(assessment.approval.approved)
        self.assertEqual(self.state.result.review_status, "partial")
        approvals = [
            item for item in self.state.reviews if item.get("state") == "APPROVED"
        ]
        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0]["commit_id"], HEAD)
        self.assertIn("AI result status remains partial", approvals[0]["body"])
        self.assertIn("mixed coverage sufficient", approvals[0]["body"])

    def test_blanket_sentence_approves_every_file_for_a_maintainer(self):
        identifier, request = self.request_review()
        self.state.permission = "maintain"
        self.state.comments[20] = {
            "id": 20,
            "user": ALICE,
            "author_association": "MEMBER",
            "issue_url": f"{API}{REPO}/issues/2",
            "updated_at": "2026-10-10T01:00:00Z",
            "body": "@reviewsensei I reviewed the media files and I approve",
        }
        self.publisher().confirm(
            request_comment_id=identifier,
            source_comment_id=20,
            result=self.state.result,
        )
        assessment = self.publisher().publish_mixed_approval(
            request_comment_id=identifier,
            result=self.state.result,
            has_open_review_threads=False,
        )
        self.assertTrue(assessment.approval.approved)
        self.assertEqual(
            set(assessment.human_reviewed_ids), {item.file_id for item in request.files}
        )
        self.assertEqual(self.state.result.review_status, "partial")
        self.assertEqual(
            [item["state"] for item in self.state.reviews if item["id"] != 10],
            ["APPROVED"],
        )

    def test_blanket_sentence_refuses_write_permission(self):
        identifier, _request = self.request_review()
        self.state.permission = "write"
        self.state.comments[20] = {
            "id": 20,
            "user": ALICE,
            "author_association": "COLLABORATOR",
            "issue_url": f"{API}{REPO}/issues/2",
            "updated_at": "2026-10-10T01:00:00Z",
            "body": "@reviewsensei I reviewed the media files and I approve",
        }
        with self.assertRaisesRegex(ReviewInputError, "maintainer or admin"):
            self.publisher().approve_reviewed_media(
                request_comment_id=identifier,
                source_comment_id=20,
                result=self.state.result,
            )

    def test_default_policy_rejects_confirmation_before_any_io(self):
        with self.assertRaisesRegex(ReviewInputError, "disabled"):
            self.publisher(enabled=False).confirm(
                request_comment_id=1, source_comment_id=2, result=self.state.result
            )
        self.assertEqual(self.state.calls, [])

    def test_repeated_source_mention_returns_exact_receipt_without_another_write(self):
        identifier, _, receipt = self.confirmed()
        count = self.posts()
        self.assertEqual(
            self.publisher().confirm(
                request_comment_id=identifier,
                source_comment_id=20,
                result=self.state.result,
            ),
            receipt,
        )
        self.assertEqual(self.posts(), count)

    def test_changed_selection_in_same_comment_cannot_rebind_event(self):
        identifier, request, _ = self.confirmed()
        self.state.source(request, (request.files[0].file_id,))
        count = self.posts()
        with self.assertRaisesRegex(ReviewInputError, "bound differently"):
            self.publisher().confirm(
                request_comment_id=identifier,
                source_comment_id=20,
                result=self.state.result,
            )
        self.assertEqual(self.posts(), count)

    def test_deleted_source_and_revoked_access_invalidate_human_coverage(self):
        for mutation in (
            "deleted",
            "revoked",
            "body",
            "timestamp",
            "actor",
            "association",
            "moved",
        ):
            with self.subTest(mutation=mutation):
                self.setUp()
                identifier, _, receipt = self.confirmed()
                if mutation == "deleted":
                    del self.state.comments[20]
                elif mutation == "revoked":
                    self.state.permission = "read"
                elif mutation == "body":
                    self.state.comments[20]["body"] += " extra prose"
                elif mutation == "timestamp":
                    self.state.comments[20]["updated_at"] = "2026-10-11T01:00:00Z"
                elif mutation == "actor":
                    self.state.comments[20]["user"] = {**ALICE, "id": 124}
                elif mutation == "association":
                    self.state.comments[20]["author_association"] = "NONE"
                else:
                    self.state.comments[20]["issue_url"] = f"{API}{REPO}/issues/3"
                outcome = self.evaluate(identifier)
                self.assertFalse(outcome.approval.approved)
                self.assertEqual(len(outcome.pending_ids), 2)
                self.assertEqual(outcome.invalidated_receipt_ids, (receipt,))
                self.assertIn(receipt, self.state.comments)

    def test_fresh_comment_can_renew_invalidated_files_without_erasing_old_audit(self):
        identifier, request, receipt = self.confirmed()
        del self.state.comments[20]
        self.state.source(request, identifier=21)
        new_receipt = self.publisher().confirm(
            request_comment_id=identifier,
            source_comment_id=21,
            result=self.state.result,
        )
        outcome = self.evaluate(identifier)
        self.assertTrue(outcome.approval.approved)
        self.assertEqual(outcome.invalidated_receipt_ids, (receipt,))
        self.assertEqual(set(outcome.receipt_ids), {receipt, new_receipt})

    def test_unauthorized_bot_and_numeric_actor_substitution_never_publish(self):
        for replacement in (
            "association",
            "bot",
            "permission",
            "numeric",
            "float",
            "missing",
        ):
            with self.subTest(replacement=replacement):
                self.setUp()
                identifier, request = self.request_review()
                self.state.source(request)
                if replacement == "association":
                    self.state.comments[20]["author_association"] = "NONE"
                elif replacement == "bot":
                    self.state.comments[20]["user"] = {**ALICE, "type": "Bot"}
                elif replacement == "permission":
                    self.state.permission = "read"
                elif replacement == "numeric":
                    self.state.comments[20]["user"] = {**ALICE, "id": 124}
                elif replacement == "float":
                    self.state.comments[20]["user"] = {**ALICE, "id": 123.0}
                else:
                    del self.state.comments[20]
                count = self.posts()
                with self.assertRaises(ReviewInputError):
                    self.publisher().confirm(
                        request_comment_id=identifier,
                        source_comment_id=20,
                        result=self.state.result,
                    )
                self.assertEqual(self.posts(), count)

    def test_unchanged_blob_does_not_carry_across_head_or_base_updates(self):
        for field in ("head", "base"):
            with self.subTest(field=field):
                self.setUp()
                identifier, _, _ = self.confirmed()
                setattr(self.state, field, "f" * 40)
                count = self.posts()
                with self.assertRaises(ReviewInputError):
                    self.evaluate(identifier)
                self.assertEqual(self.posts(), count)

    def test_changed_blob_mode_and_new_binary_invalidate_inventory(self):
        for mutation in ("blob", "mode", "new"):
            with self.subTest(mutation=mutation):
                self.setUp()
                identifier, _, _ = self.confirmed()
                if mutation == "blob":
                    self.state.new["photo.png"] = ("f" * 40, "100644")
                elif mutation == "mode":
                    self.state.new["photo.png"] = ("d" * 40, "100755")
                else:
                    self.state.files.append({"filename": "new.png", "status": "added"})
                    self.state.new["new.png"] = ("f" * 40, "100644")
                with self.assertRaises(ReviewInputError):
                    self.evaluate(identifier)

    def test_renamed_and_deleted_binaries_bind_old_and_new_regular_blobs(self):
        for change in ("renamed", "removed"):
            with self.subTest(change=change):
                self.setUp()
                self.state.files = [
                    {
                        "filename": "new.png" if change == "renamed" else "photo.png",
                        "status": change,
                    }
                ]
                if change == "renamed":
                    self.state.files[0]["previous_filename"] = "photo.png"
                    self.state.new = {"new.png": self.state.old["photo.png"]}
                    paths = ("photo.png", "new.png")
                else:
                    self.state.new = {}
                    paths = ("photo.png",)
                coverage = CoverageManifest(
                    files=tuple(
                        FileCoverage(p, "unsupported", "binary") for p in paths
                    ),
                    enumerated_paths=paths,
                )
                self.state.set_result(replace(self.state.result, coverage=coverage))
                identifier, request, _ = self.confirmed()
                self.assertEqual(request.files[0].change, change)
                self.assertTrue(self.evaluate(identifier).approval.approved)
                if change == "renamed":
                    self.assertEqual(request.files[0].old_path, "photo.png")
                    self.assertEqual(
                        request.files[0].new_blob, request.files[0].old_blob
                    )
                else:
                    self.assertIsNone(request.files[0].new_blob)

    def test_blocking_text_finding_and_other_original_approval_facts_remain(self):
        cases = [
            ({"enabled": False}, None, "auto-approval-disabled"),
            ({"app_authored": True}, None, "app-authored-pull-request"),
            ({"qualification": "missing"}, None, "qualification-missing"),
            (
                {},
                ReviewComment("app.py", 1, "Fix the text defect.", blocking=True),
                "blocking-findings-open",
            ),
        ]
        for facts, comment, blocker in cases:
            with self.subTest(blocker=blocker):
                self.setUp()
                result = (
                    replace(self.state.result, comments=(comment,))
                    if comment
                    else self.state.result
                )
                self.state.set_result(result, **facts)
                identifier, _, _ = self.confirmed()
                outcome = self.evaluate(identifier)
                self.assertFalse(outcome.approval.approved)
                self.assertIn(blocker, outcome.approval.blockers)
                self.assertEqual(
                    self.state.result.comments, (comment,) if comment else ()
                )

    def test_missing_provenance_and_failed_text_coverage_cannot_be_cleared(self):
        for change in ("provenance", "failed-text"):
            with self.subTest(change=change):
                self.setUp()
                result = replace(self.state.result, coverage_only_partial=False)
                if change == "failed-text":
                    result = replace(
                        result,
                        coverage=replace(
                            result.coverage,
                            files=(
                                result.coverage.files[0],
                                result.coverage.files[1],
                                FileCoverage(
                                    "app.py", "budget-exhausted", "provider-call-budget"
                                ),
                            ),
                        ),
                    )
                self.state.set_result(result)
                identifier, _, _ = self.confirmed()
                outcome = self.evaluate(identifier)
                self.assertFalse(outcome.approval.approved)
                self.assertIn("review-partial", outcome.approval.blockers)

    def test_unresolved_or_unknown_threads_still_withhold(self):
        identifier, _, _ = self.confirmed()
        for value in (True, None):
            outcome = self.publisher().evaluate(
                request_comment_id=identifier,
                result=self.state.result,
                has_open_review_threads=value,
            )
            self.assertFalse(outcome.approval.approved)

    def test_latest_broken_owned_root_refuses_older_clean_authority(self):
        identifier, _, _ = self.confirmed()
        self.state.reviews.append(
            {
                **self.state.root,
                "id": 11,
                "body": "<!-- reviewsensei:approval-eligibility:v1 invalid -->",
            }
        )
        # Use the actual eligibility prefix so the malformed latest candidate
        # is recognized as authority rather than unrelated conversation.
        from review_sensei.hosting.github.publication import (
            APPROVAL_ELIGIBILITY_MARKER_PREFIX,
        )

        self.state.reviews[-1]["body"] = (
            APPROVAL_ELIGIBILITY_MARKER_PREFIX + " invalid -->"
        )
        with self.assertRaises(ReviewInputError):
            self.evaluate(identifier)

    def test_human_pasted_receipt_is_reference_data_not_authority(self):
        identifier, _, receipt = self.confirmed()
        self.state.comments[receipt]["user"] = ALICE
        outcome = self.evaluate(identifier)
        self.assertFalse(outcome.approval.approved)
        self.assertEqual(outcome.human_reviewed_ids, ())

    def test_shared_io_exhaustion_prevents_dispatch_and_preserves_deadline(self):
        identifier, request = self.request_review()
        self.state.source(request)
        publisher = self.publisher()
        for _ in range(60):
            self.budget.consume()
        snapshot = self.budget.snapshot()
        count = len(self.state.calls)
        with self.assertRaises(ReviewInputError):
            publisher.confirm(
                request_comment_id=identifier,
                source_comment_id=20,
                result=self.state.result,
            )
        self.assertEqual(len(self.state.calls), count)
        self.assertEqual(self.budget.snapshot(), snapshot)

    def test_head_changed_after_receipt_write_never_acknowledges_success(self):
        identifier, request = self.request_review()
        self.state.source(request)

        def moved(method, path):
            if method == "POST":
                self.state.head = "f" * 40

        self.state.hook = moved
        with self.assertRaises(ReviewInputError):
            self.publisher().confirm(
                request_comment_id=identifier,
                source_comment_id=20,
                result=self.state.result,
            )
        self.assertEqual(self.posts(), 2)  # request plus non-authoritative audit

    def test_changed_result_or_owned_root_needs_a_new_request(self):
        identifier, _, _ = self.confirmed()
        self.state.set_result(
            replace(self.state.result, summary="Different text analysis.")
        )
        with self.assertRaises(ReviewInputError):
            self.evaluate(identifier)

    def test_nonregular_or_missing_immutable_blob_refuses_before_publication(self):
        for mode in ("120000", "160000", "040000", "100644-missing"):
            with self.subTest(mode=mode):
                self.setUp()
                if mode.endswith("missing"):
                    del self.state.new["photo.png"]
                else:
                    self.state.new["photo.png"] = ("d" * 40, mode)
                with self.assertRaises(ReviewInputError):
                    self.request_review()
                self.assertEqual(self.posts(), 0)

    def test_truncated_tree_and_omitted_text_inventory_refuse(self):
        real = self.state.request

        def truncated(method, path, **kwargs):
            status, value = real(method, path, **kwargs)
            if "/git/trees/" in path:
                value["truncated"] = True
            return status, value

        self.state.request = truncated
        with self.assertRaises(ReviewInputError):
            self.request_review()
        self.setUp()
        self.state.files.append({"filename": "unreviewed.py", "status": "added"})
        with self.assertRaisesRegex(ReviewInputError, "omits or adds"):
            self.request_review()
        self.assertEqual(self.posts(), 0)

    def test_unknown_post_response_reconciles_exact_owned_audit_without_retry(self):
        identifier, request = self.request_review()
        self.state.source(request)
        real = self.state.request

        def lost_response(method, path, **kwargs):
            status, value = real(method, path, **kwargs)
            return (502, {}) if method == "POST" else (status, value)

        self.state.request = lost_response
        with self.assertRaises(ReviewInputError):
            self.publisher().confirm(
                request_comment_id=identifier,
                source_comment_id=20,
                result=self.state.result,
            )
        self.assertEqual(self.posts(), 2)
        self.state.request = real
        receipt = self.publisher().confirm(
            request_comment_id=identifier,
            source_comment_id=20,
            result=self.state.result,
        )
        self.assertEqual(receipt, self.state.next_id)
        self.assertEqual(self.posts(), 2)
        self.assertTrue(self.evaluate(identifier).approval.approved)

    def test_request_retry_reuses_owned_exact_body_and_default_explains_disabled_policy(
        self,
    ):
        publisher = self.publisher(enabled=False)
        identifier, request = publisher.request_review(
            review_id=10, result=self.state.result
        )
        self.assertIn(
            "disabled by trusted host policy", self.state.comments[identifier]["body"]
        )
        self.assertEqual(
            self.publisher(enabled=False).request_review(
                review_id=10, result=self.state.result
            ),
            (identifier, request),
        )
        self.assertEqual(self.posts(), 1)

    def test_duplicate_owned_source_receipts_refuse_ambiguous_audit(self):
        identifier, _, receipt = self.confirmed()
        self.state.comments[999] = {**self.state.comments[receipt], "id": 999}
        with self.assertRaisesRegex(ReviewInputError, "conflicting"):
            self.evaluate(identifier)

    def test_expired_original_invocation_stops_before_any_http(self):
        identifier, _, _ = self.confirmed()
        clock = [100.0]
        budget = EvidenceReadBudget(clock=lambda: clock[0])
        publisher = HumanFileReviewPublisher(
            http=self.state,
            api_url=API,
            repository="owner/repo",
            repository_id=1,
            pull_request=2,
            app_login=BOT["login"],
            app_user_id=BOT["id"],
            token="synthetic",
            before_request=budget.consume,
            policy=HumanFileReviewPolicy(True),
        )
        clock[0] = 160.0
        calls = len(self.state.calls)
        with self.assertRaises(ReviewInputError):
            publisher.evaluate(
                request_comment_id=identifier,
                result=self.state.result,
                has_open_review_threads=False,
            )
        self.assertEqual(len(self.state.calls), calls)

    def test_source_edit_during_reacquisition_is_invalidated_by_late_fence(self):
        identifier, _, receipt = self.confirmed()
        reads = [0]

        def edited(method, path):
            if path.endswith("/issues/comments/20"):
                reads[0] += 1
            if "/git/commits/" in path and reads[0]:
                self.state.comments[20]["body"] += " edited"

        self.state.hook = edited
        assessment = self.evaluate(identifier)
        self.assertFalse(assessment.approval.approved)
        self.assertEqual(assessment.invalidated_receipt_ids, (receipt,))
