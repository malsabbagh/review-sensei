"""Public adapter journeys against a mutable synthetic GitHub authority.

Each request/confirmation/evaluation is a separate explicit host invocation
(humans can reply days later). Within an invocation the original budget object
is never replaced. No inference, broker or whole hosted 64/60 claim is made.
"""

import copy
import unittest
from dataclasses import replace

from review_sensei_github.approval import (
    ReviewApprovalEligibility,
    approval_facts_from_result,
)
from review_sensei_github.human_file_review import (
    HumanFileReviewPolicy,
    HumanFileReviewPublisher,
)
from review_sensei_github.publication import (
    approval_eligibility_marker,
    review_result_digest,
)

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.coverage import CoverageManifest, FileCoverage
from review_sensei.errors import ReviewInputError
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
                "base": {
                    "sha": self.base,
                    "repo": {"id": 1, "full_name": "owner/repo"},
                },
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
            tree, _, query = path.rsplit("/", 1)[1].partition("?")
            info = None
            for blobs, root, offset in (
                (self.old, "1" * 40, 10000),
                (self.new, "2" * 40, 20000),
            ):
                ancestors = sorted(
                    {
                        "/".join(parts[:index])
                        for name in blobs
                        for parts in [name.split("/")]
                        for index in range(1, len(parts))
                    }
                )
                identifiers = {
                    "": root,
                    **{
                        name: f"{offset + index:040x}"
                        for index, name in enumerate(ancestors)
                    },
                }
                scope = next(
                    (name for name, sha in identifiers.items() if sha == tree), None
                )
                if scope is not None:
                    info = (blobs, identifiers, scope)
                    break
            if info is None:
                raise AssertionError(path)
            blobs, identifiers, scope = info
            prefix = scope + "/" if scope else ""
            entries = []
            for name, (sha, mode) in blobs.items():
                if name.startswith(prefix):
                    relative = name[len(prefix) :]
                    if query == "recursive=1" or "/" not in relative:
                        entries.append(
                            {"path": relative, "type": "blob", "sha": sha, "mode": mode}
                        )
            for name, sha in identifiers.items():
                if name and name != scope and name.startswith(prefix):
                    relative = name[len(prefix) :]
                    if query == "recursive=1" or "/" not in relative:
                        entries.append(
                            {
                                "path": relative,
                                "type": "tree",
                                "sha": sha,
                                "mode": "040000",
                            }
                        )
            value = {"sha": tree, "truncated": False, "tree": entries}
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
        from review_sensei_github.publication import (
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
            if "/pulls/2/reviews?" in path and reads[0]:
                self.state.comments[20]["body"] += " edited"

        self.state.hook = edited
        assessment = self.evaluate(identifier)
        self.assertFalse(assessment.approval.approved)
        self.assertEqual(assessment.invalidated_receipt_ids, (receipt,))

    def blanket_source(self):
        self.state.permission = "maintain"
        self.state.comments[20] = {
            "id": 20,
            "user": ALICE,
            "author_association": "MEMBER",
            "issue_url": f"{API}{REPO}/issues/2",
            "updated_at": "2026-10-10T01:00:00Z",
            "body": "@reviewsensei I reviewed the media files and I approve",
        }

    def many_files(self, count, nested=False):
        buckets = (
            "after/desktop",
            "after/mobile",
            "before/desktop",
            "before/mobile",
            "zoom",
        )
        paths = tuple(
            f"docs/execution/quieter-workspace/{buckets[n // 10]}/media-{n:03}.png"
            if nested
            else f"media-{n:03}.png"
            for n in range(count)
        )
        self.state.old = (
            {"docs/execution/existing-not-media.txt": ("f" * 40, "100644")}
            if nested
            else {}
        )
        self.state.new = {
            path: (f"{n + 100:040x}", "100644") for n, path in enumerate(paths)
        }
        self.state.new.update(self.state.old)
        self.state.files = [{"filename": path, "status": "added"} for path in paths]
        self.state.set_result(
            replace(
                self.state.result,
                coverage=CoverageManifest(
                    files=tuple(
                        FileCoverage(path, "unsupported", "binary") for path in paths
                    ),
                    enumerated_paths=paths,
                ),
            )
        )

    def test_nested_forty_one_files_use_complete_recursive_tree_proof(self):
        self.many_files(41, nested=True)
        self.blanket_source()
        publisher = self.publisher()
        identifier, request = publisher.request_review(
            review_id=10, result=self.state.result
        )
        assessment = publisher.approve_reviewed_media(
            request_comment_id=identifier,
            source_comment_id=20,
            result=self.state.result,
            has_open_review_threads=False,
        )
        self.assertTrue(assessment.approval.approved)
        self.assertEqual(len(assessment.human_reviewed_ids), 41)
        self.assertEqual(
            {item.path for item in request.files},
            {item["filename"] for item in self.state.files},
        )
        self.assertEqual(self.budget.calls, len(self.state.calls))
        self.assertEqual(self.budget.calls, 33)
        trees = [path for _, path, _ in self.state.calls if "/git/trees/" in path]
        self.assertEqual(
            trees,
            [
                f"{REPO}/git/trees/{'1' * 40}",
                f"{REPO}/git/trees/{10000:040x}?recursive=1",
                f"{REPO}/git/trees/{'2' * 40}",
                f"{REPO}/git/trees/{20000:040x}?recursive=1",
            ],
        )
        self.assertEqual([len(part.files) for part in request.parts], [32, 9])
        self.assertEqual(self.state.result.review_status, "partial")

    def test_nested_scoped_read_never_attempts_oversized_whole_repository_tree(self):
        self.many_files(41, nested=True)
        self.blanket_source()
        real = self.state.request

        def root_too_large(method, path, **kwargs):
            status, value = real(method, path, **kwargs)
            if path.endswith(f"/git/trees/{'2' * 40}?recursive=1"):
                value["padding"] = "x" * (512 * 1024)
            return status, value

        self.state.request = root_too_large
        publisher = self.publisher()
        identifier, _ = publisher.request_review(review_id=10, result=self.state.result)
        outcome = publisher.approve_reviewed_media(
            request_comment_id=identifier,
            source_comment_id=20,
            result=self.state.result,
            has_open_review_threads=False,
        )
        self.assertTrue(outcome.approval.approved)
        self.assertFalse(
            any(
                path.endswith(f"/git/trees/{root}?recursive=1")
                for _, path, _ in self.state.calls
                for root in ("1" * 40, "2" * 40)
            )
        )
        self.assertEqual(self.budget.calls, 33)

    def test_nested_shallow_prefix_requires_complete_regular_directory_chain(self):
        for mutation in (
            "truncated",
            "sha",
            "duplicate",
            "non-immediate",
            "symlink",
            "submodule",
            "missing",
        ):
            with self.subTest(mutation=mutation):
                self.setUp()
                self.many_files(41, nested=True)
                real = self.state.request

                def broken(method, path, **kwargs):
                    status, value = real(method, path, **kwargs)
                    if path.endswith(f"/git/trees/{'2' * 40}"):
                        if mutation == "truncated":
                            value["truncated"] = True
                        elif mutation == "sha":
                            value["sha"] = "f" * 40
                        elif mutation == "duplicate":
                            value["tree"].append(dict(value["tree"][0]))
                        elif mutation == "non-immediate":
                            value["tree"][0]["path"] = "docs/execution"
                        elif mutation in {"symlink", "submodule"}:
                            value["tree"][0].update(
                                type="blob" if mutation == "symlink" else "commit",
                                mode="120000" if mutation == "symlink" else "160000",
                            )
                        else:
                            value["tree"] = []
                    return status, value

                self.state.request = broken
                publisher = self.publisher()
                with self.assertRaises(ReviewInputError):
                    publisher.request_review(review_id=10, result=self.state.result)
                self.assertEqual(self.posts(), 0)
                self.assertEqual(self.budget.calls, len(self.state.calls))
                self.assertFalse(
                    any(
                        path == f"{REPO}/git/trees/{20000:040x}?recursive=1"
                        for _, path, _ in self.state.calls
                    )
                )

    def test_nested_recursive_tree_refuses_incomplete_or_conflicting_proof(self):
        for mutation in (
            "truncated",
            "tree-sha",
            "list-shape",
            "entry-shape",
            "missing-ancestor",
            "symlink",
            "ancestor-blob",
            "duplicate",
            "traversal",
            "bad-mode",
            "bad-type",
            "missing-leaf",
            "response-bound",
        ):
            with self.subTest(mutation=mutation):
                self.setUp()
                self.many_files(41, nested=True)
                self.blanket_source()
                real = self.state.request

                def invalid(method, path, **kwargs):
                    status, value = real(method, path, **kwargs)
                    if path == f"{REPO}/git/trees/{20000:040x}?recursive=1":
                        if mutation == "truncated":
                            value["truncated"] = True
                        elif mutation == "tree-sha":
                            value["sha"] = "f" * 40
                        elif mutation == "list-shape":
                            value["tree"] = {}
                        elif mutation == "entry-shape":
                            value["tree"].append(None)
                        elif mutation == "missing-ancestor":
                            value["tree"] = [
                                x
                                for x in value["tree"]
                                if x["path"] != "execution/quieter-workspace/after"
                            ]
                        elif mutation == "symlink":
                            value["tree"][0]["mode"] = "120000"
                        elif mutation == "ancestor-blob":
                            ancestor = next(
                                x
                                for x in value["tree"]
                                if x["path"] == "execution/quieter-workspace/after"
                            )
                            ancestor.update(type="blob", mode="100644")
                        elif mutation == "duplicate":
                            value["tree"].append(dict(value["tree"][0]))
                        elif mutation == "traversal":
                            value["tree"].append(
                                dict(
                                    path="docs/../escape.png",
                                    type="blob",
                                    mode="100644",
                                    sha="f" * 40,
                                )
                            )
                        elif mutation == "bad-mode":
                            value["tree"][0]["mode"] = "100777"
                        elif mutation == "bad-type":
                            value["tree"][0]["type"] = "tree"
                        elif mutation == "missing-leaf":
                            value["tree"] = [
                                item
                                for item in value["tree"]
                                if item["path"]
                                != "execution/quieter-workspace/after/desktop/media-000.png"
                            ]
                        else:
                            value["padding"] = "x" * (512 * 1024)
                    return status, value

                self.state.request = invalid
                publisher = self.publisher()
                with self.assertRaises(ReviewInputError):
                    publisher.request_review(review_id=10, result=self.state.result)
                self.assertEqual(self.budget.calls, len(self.state.calls))
                self.assertEqual(self.posts(), 0)
                self.assertFalse(
                    any(x["state"] == "APPROVED" for x in self.state.reviews)
                )
                self.assertEqual(
                    len(
                        [
                            path
                            for _, path, _ in self.state.calls
                            if "?recursive=1" in path
                        ]
                    ),
                    2,
                )

    def test_nested_absent_old_path_requires_unconflicted_ancestor_proof(self):
        for conflict in (False, True):
            with self.subTest(conflict=conflict):
                self.setUp()
                self.many_files(41, nested=True)
                self.state.old = {
                    "docs" if conflict else "unrelated-link": ("f" * 40, "120000")
                }
                publisher = self.publisher()
                if conflict:
                    with self.assertRaisesRegex(ReviewInputError, "regular tree path"):
                        publisher.request_review(review_id=10, result=self.state.result)
                    self.assertEqual(self.posts(), 0)
                else:
                    _, request = publisher.request_review(
                        review_id=10, result=self.state.result
                    )
                    self.assertTrue(all(x.old_blob is None for x in request.files))

    def test_nested_recursive_dispatch_respects_original_exhaustion_without_approval(
        self,
    ):
        self.many_files(41, nested=True)
        self.blanket_source()
        publisher = self.publisher()
        for _ in range(51):
            self.budget.consume()
        with self.assertRaises(ReviewInputError):
            publisher.request_review(review_id=10, result=self.state.result)
        self.assertEqual(self.budget.calls, 60)
        self.assertEqual(len(self.state.calls), 9)
        self.assertEqual(
            self.state.calls[-1][1], f"{REPO}/git/trees/{20000:040x}?recursive=1"
        )
        self.assertEqual(self.posts(), 0)
        self.assertFalse(any(x["state"] == "APPROVED" for x in self.state.reviews))

    def test_compound_request_confirmation_approval_fits_one_original_budget(self):
        for count in (2, 41):
            with self.subTest(count=count):
                self.setUp()
                if count == 41:
                    self.many_files(count)
                self.blanket_source()
                publisher = self.publisher()
                identifier, request = publisher.request_review(
                    review_id=10, result=self.state.result
                )
                assessment = publisher.approve_reviewed_media(
                    request_comment_id=identifier,
                    source_comment_id=20,
                    result=self.state.result,
                    has_open_review_threads=False,
                )
                self.assertTrue(assessment.approval.approved)
                self.assertEqual(len(assessment.human_reviewed_ids), count)
                self.assertEqual(self.budget.calls, len(self.state.calls))
                self.assertEqual(self.budget.calls, 31)
                self.assertEqual(self.state.result.review_status, "partial")
                self.assertEqual(
                    len([x for x in self.state.reviews if x["state"] == "APPROVED"]), 1
                )
                if count == 41:
                    self.assertEqual(
                        [len(part.files) for part in request.parts], [32, 9]
                    )
                    self.assertIn(
                        "request-group:v1", self.state.comments[identifier]["body"]
                    )
                    self.assertTrue(
                        any(
                            "receipt-group:v1" in x["body"]
                            for x in self.state.comments.values()
                        )
                    )
                posts = self.posts()
                publisher = self.publisher()
                self.assertEqual(
                    publisher.request_review(review_id=10, result=self.state.result)[0],
                    identifier,
                )
                retried = publisher.approve_reviewed_media(
                    request_comment_id=identifier,
                    source_comment_id=20,
                    result=self.state.result,
                    has_open_review_threads=False,
                )
                self.assertTrue(retried.approval.approved)
                self.assertEqual(self.posts(), posts)

    def test_compound_unknown_receipt_or_approval_write_reconciles_without_duplicate(
        self,
    ):
        for failed_path in ("/issues/2/comments", "/pulls/2/reviews"):
            with self.subTest(failed_path=failed_path):
                self.setUp()
                self.blanket_source()
                publisher = self.publisher()
                identifier, _request = publisher.request_review(
                    review_id=10, result=self.state.result
                )
                real = self.state.request

                def lost(method, path, **kwargs):
                    status, value = real(method, path, **kwargs)
                    return (
                        (502, {})
                        if method == "POST" and path.endswith(failed_path)
                        else (status, value)
                    )

                self.state.request = lost
                with self.assertRaises(ReviewInputError):
                    publisher.approve_reviewed_media(
                        request_comment_id=identifier,
                        source_comment_id=20,
                        result=self.state.result,
                        has_open_review_threads=False,
                    )
                self.state.request = real
                publisher = self.publisher()
                publisher.request_review(review_id=10, result=self.state.result)
                assessment = publisher.approve_reviewed_media(
                    request_comment_id=identifier,
                    source_comment_id=20,
                    result=self.state.result,
                    has_open_review_threads=False,
                )
                self.assertTrue(assessment.approval.approved)
                self.assertEqual(
                    len(self.state.comments), 3
                )  # source, request, receipt
                self.assertEqual(
                    len([x for x in self.state.reviews if x["state"] == "APPROVED"]), 1
                )

    def test_compound_late_mutable_changes_never_approve(self):
        for mutation in (
            "head",
            "base",
            "source",
            "permission",
            "request",
            "root",
            "duplicate",
        ):
            with self.subTest(mutation=mutation):
                self.setUp()
                self.blanket_source()
                publisher = self.publisher()
                identifier, _ = publisher.request_review(
                    review_id=10, result=self.state.result
                )
                fired = [False]

                def changed(method, path):
                    if (
                        fired[0]
                        or method != "POST"
                        or not path.endswith("/issues/2/comments")
                    ):
                        return
                    fired[0] = True
                    if mutation in {"head", "base"}:
                        setattr(self.state, mutation, "f" * 40)
                    elif mutation == "source":
                        self.state.comments[20]["body"] += " edited"
                    elif mutation == "permission":
                        self.state.permission = "read"
                    elif mutation == "request":
                        self.state.comments[identifier]["body"] += " edited"
                    elif mutation == "root":
                        self.state.reviews.append({**self.state.root, "id": 1000})

                if mutation == "duplicate":
                    real = self.state.request

                    def duplicated(method, path, **kwargs):
                        status, value = real(method, path, **kwargs)
                        if method == "POST" and path.endswith("/issues/2/comments"):
                            self.state.comments[999] = {**value, "id": 999}
                        return status, value

                    self.state.request = duplicated
                else:
                    self.state.hook = changed
                try:
                    result = publisher.approve_reviewed_media(
                        request_comment_id=identifier,
                        source_comment_id=20,
                        result=self.state.result,
                        has_open_review_threads=False,
                    )
                except ReviewInputError:
                    pass
                else:
                    self.assertFalse(result.approval.approved)
                self.assertFalse(
                    any(x["state"] == "APPROVED" for x in self.state.reviews)
                )

    def test_compound_fresh_host_facts_withhold_or_refuse_before_approve(self):
        for facts in (
            {
                "enabled": True,
                "app_authored": False,
                "has_open_review_threads": True,
                "qualification": "not-required",
            },
            {
                "enabled": True,
                "app_authored": True,
                "has_open_review_threads": False,
                "qualification": "not-required",
            },
            {
                "enabled": True,
                "app_authored": False,
                "has_open_review_threads": False,
                "qualification": "missing",
            },
            {
                "enabled": True,
                "app_authored": False,
                "has_open_review_threads": False,
                "qualification": "not-required",
                "unexpected": True,
            },
        ):
            with self.subTest(facts=facts):
                self.setUp()
                self.blanket_source()
                publisher = self.publisher()
                publisher.approval_facts_provider = lambda **kwargs: facts
                identifier, _ = publisher.request_review(
                    review_id=10, result=self.state.result
                )
                try:
                    result = publisher.approve_reviewed_media(
                        request_comment_id=identifier,
                        source_comment_id=20,
                        result=self.state.result,
                        has_open_review_threads=False,
                    )
                except ReviewInputError:
                    pass
                else:
                    self.assertFalse(result.approval.approved)
                self.assertFalse(
                    any(x["state"] == "APPROVED" for x in self.state.reviews)
                )

    def test_native_tail_is_admitted_before_post_and_retry_reuses_recorded_receipt(
        self,
    ):
        self.blanket_source()
        publisher = self.publisher()
        budget = self.budget
        calls = []

        def admit(required):
            calls.append((required, budget.calls))
            budget.check()
            if budget.calls + required > 60:
                raise ReviewInputError(
                    "native approval tail does not fit original budget"
                )

        publisher.before_approval = admit
        # Earlier broker/metadata work belongs to this same original allowance.
        for _ in range(31):
            budget.consume()
        identifier, _ = publisher.request_review(review_id=10, result=self.state.result)
        with self.assertRaisesRegex(ReviewInputError, "tail does not fit"):
            publisher.approve_reviewed_media(
                request_comment_id=identifier,
                source_comment_id=20,
                result=self.state.result,
                has_open_review_threads=False,
            )
        self.assertEqual(calls, [(3, 59)])
        self.assertEqual(budget.calls, 59)  # refusal neither debits nor refunds
        self.assertEqual(len(self.state.comments), 3)  # source/request/retained receipt
        self.assertFalse(any(x["state"] == "APPROVED" for x in self.state.reviews))
        posts = self.posts()
        # A later explicit invocation owns a new original event allowance, not
        # a refill inside the refused invocation. It authenticates/reuses audit.
        publisher = self.publisher()
        publisher.request_review(review_id=10, result=self.state.result)
        result = publisher.approve_reviewed_media(
            request_comment_id=identifier,
            source_comment_id=20,
            result=self.state.result,
            has_open_review_threads=False,
        )
        self.assertTrue(result.approval.approved)
        self.assertEqual(self.posts(), posts + 1)
        self.assertEqual(len(self.state.comments), 3)
        self.assertEqual(
            len([x for x in self.state.reviews if x["state"] == "APPROVED"]), 1
        )

    def test_existing_native_approval_requires_funded_two_call_readback_tail(self):
        self.blanket_source()
        publisher = self.publisher()
        identifier, _ = publisher.request_review(review_id=10, result=self.state.result)
        publisher.approve_reviewed_media(
            request_comment_id=identifier,
            source_comment_id=20,
            result=self.state.result,
            has_open_review_threads=False,
        )
        posts = self.posts()
        publisher = self.publisher()
        publisher.request_review(review_id=10, result=self.state.result)
        seen = []

        def refuse(required):
            seen.append(required)
            raise ReviewInputError("existing native tail is unfunded")

        publisher.before_approval = refuse
        with self.assertRaisesRegex(ReviewInputError, "existing native tail"):
            publisher.approve_reviewed_media(
                request_comment_id=identifier,
                source_comment_id=20,
                result=self.state.result,
                has_open_review_threads=False,
            )
        self.assertEqual(seen, [2])
        self.assertEqual(self.posts(), posts)

    def test_current_finding_facts_bind_resolved_inline_without_editing_retained_result(
        self,
    ):
        from review_sensei.mixed_coverage import CurrentFindingFacts

        comment = ReviewComment(
            "app.py", 1, "An inline finding retained for audit.", blocking=True
        )
        result = replace(self.state.result, comments=(comment,))
        self.state.set_result(result, has_blocking_findings=False)
        before = result.to_dict()
        digest = result.content_digest()
        self.blanket_source()
        publisher = self.publisher()
        current = CurrentFindingFacts(digest, False, False)
        publisher.approval_facts_provider = lambda **kwargs: dict(
            enabled=True,
            app_authored=False,
            has_open_review_threads=False,
            qualification="not-required",
            current_findings=current,
        )
        identifier, _ = publisher.request_review(review_id=10, result=result)
        assessment = publisher.approve_reviewed_media(
            request_comment_id=identifier,
            source_comment_id=20,
            result=result,
            has_open_review_threads=False,
        )
        self.assertTrue(assessment.approval.approved)
        self.assertIs(assessment.current_findings, current)
        self.assertEqual(result.to_dict(), before)
        self.assertEqual(result.content_digest(), digest)
        self.assertIs(result.comments[0], comment)
        self.assertTrue(result.comments[0].blocks_approval)

    def test_current_finding_facts_never_drop_unproven_or_original_blockers(self):
        from review_sensei.mixed_coverage import CurrentFindingFacts

        for mode in ("missing", "plain-false", "stale", "frozen", "human", "blocking"):
            with self.subTest(mode=mode):
                self.setUp()
                comment = ReviewComment(
                    "app.py", 1, "Retained blocking finding.", blocking=True
                )
                result = replace(self.state.result, comments=(comment,))
                self.state.set_result(result, has_blocking_findings=mode == "frozen")
                self.blanket_source()
                publisher = self.publisher()

                def facts(**kwargs):
                    value = dict(
                        enabled=True,
                        app_authored=False,
                        has_open_review_threads=False,
                        qualification="not-required",
                    )
                    if mode != "missing":
                        value["current_findings"] = (
                            False
                            if mode == "plain-false"
                            else CurrentFindingFacts(
                                "f" * 64
                                if mode == "stale"
                                else result.content_digest(),
                                mode == "blocking",
                                mode == "human",
                            )
                        )
                    return value

                publisher.approval_facts_provider = facts
                identifier, _ = publisher.request_review(review_id=10, result=result)
                if mode in {"plain-false", "stale"}:
                    with self.assertRaisesRegex(
                        ReviewInputError, "current finding facts"
                    ):
                        publisher.approve_reviewed_media(
                            request_comment_id=identifier,
                            source_comment_id=20,
                            result=result,
                            has_open_review_threads=False,
                        )
                else:
                    assessment = publisher.approve_reviewed_media(
                        request_comment_id=identifier,
                        source_comment_id=20,
                        result=result,
                        has_open_review_threads=False,
                    )
                    self.assertFalse(assessment.approval.approved)
                self.assertFalse(
                    any(x["state"] == "APPROVED" for x in self.state.reviews)
                )

    def test_compound_rechecks_latest_root_after_current_fact_reads(self):
        self.blanket_source()
        publisher = self.publisher()
        identifier, _request = publisher.request_review(
            review_id=10, result=self.state.result
        )

        def superseded(**kwargs):
            self.state.reviews.append({**self.state.root, "id": 1000})
            return dict(
                enabled=True,
                app_authored=False,
                has_open_review_threads=False,
                qualification="not-required",
            )

        publisher.approval_facts_provider = superseded
        with self.assertRaisesRegex(ReviewInputError, "superseded"):
            publisher.approve_reviewed_media(
                request_comment_id=identifier,
                source_comment_id=20,
                result=self.state.result,
                has_open_review_threads=False,
            )
        self.assertFalse(any(x["state"] == "APPROVED" for x in self.state.reviews))

    def test_compound_final_snapshot_uses_host_validator_after_fact_reads(self):
        for mutation in ("draft", "fork", "author"):
            with self.subTest(mutation=mutation):
                self.setUp()
                self.blanket_source()
                publisher = self.publisher()
                identifier, _request = publisher.request_review(
                    review_id=10, result=self.state.result
                )
                changed = [False]

                def current(**kwargs):
                    changed[0] = True
                    return dict(
                        enabled=True,
                        app_authored=False,
                        has_open_review_threads=False,
                        qualification="not-required",
                    )

                publisher.approval_facts_provider = current
                real = self.state.request

                def response(method, path, **kwargs):
                    status, value = real(method, path, **kwargs)
                    if changed[0] and method == "GET" and path.endswith("/pulls/2"):
                        if mutation == "draft":
                            value["draft"] = True
                        elif mutation == "fork":
                            value["base"]["repo"]["fork"] = True
                        else:
                            value["user"] = BOT
                    return status, value

                def validate(payload):
                    if (
                        payload.get("draft")
                        or payload["base"]["repo"].get("fork")
                        or payload.get("user") == BOT
                    ):
                        raise ReviewInputError("host snapshot changed")

                publisher.snapshot_validator = validate
                self.state.request = response
                with self.assertRaisesRegex(ReviewInputError, "host snapshot changed"):
                    publisher.approve_reviewed_media(
                        request_comment_id=identifier,
                        source_comment_id=20,
                        result=self.state.result,
                        has_open_review_threads=False,
                    )
                self.assertFalse(
                    any(x["state"] == "APPROVED" for x in self.state.reviews)
                )

    def test_approval_readback_requires_exact_numeric_owned_review(self):
        for change in ("owner", "id", "float"):
            with self.subTest(change=change):
                self.setUp()
                self.blanket_source()
                publisher = self.publisher()
                identifier, _ = publisher.request_review(
                    review_id=10, result=self.state.result
                )
                real = self.state.request

                def altered(method, path, **kwargs):
                    status, value = real(method, path, **kwargs)
                    if (
                        method == "GET"
                        and "/pulls/2/reviews/" in path
                        and value.get("state") == "APPROVED"
                    ):
                        if change == "owner":
                            value["user"] = ALICE
                        else:
                            value["id"] = (
                                value["id"] + 1
                                if change == "id"
                                else float(value["id"])
                            )
                    return status, value

                self.state.request = altered
                with self.assertRaisesRegex(ReviewInputError, "readback disagrees"):
                    publisher.approve_reviewed_media(
                        request_comment_id=identifier,
                        source_comment_id=20,
                        result=self.state.result,
                        has_open_review_threads=False,
                    )

    def test_generated_file_is_requested_with_the_owned_review(self):
        from review_sensei_github.application import GitHubApplication

        from review_sensei.coverage import HunkCoverage

        path = "widget.min.js"
        coverage = CoverageManifest(
            files=(
                FileCoverage("app.py", "reviewed"),
                FileCoverage(path, "excluded-by-policy", "generated"),
            ),
            hunks=(HunkCoverage(1, path, "excluded-by-policy", "generated"),),
            enumerated_paths=("app.py", path),
        )
        result = ReviewResult(
            "Completed text analysis; generated files need human review.",
            (),
            "fixture",
            review_status="partial",
            coverage=coverage,
            coverage_only_partial=True,
        )
        self.state.set_result(result)
        self.state.files = [
            {"filename": "app.py", "status": "modified"},
            {"filename": path, "status": "added"},
        ]
        self.state.new[path] = ("9" * 40, "100644")

        class Http:
            api_url = API

            def __init__(self, state):
                self.state = state

            def request(
                self, method, path, token=None, body=None, timeout_seconds=None
            ):
                if path == "/user":
                    return 200, {"id": BOT["id"], "login": BOT["login"], "type": "Bot"}
                return self.state.request(
                    method, path, body=body, timeout_seconds=timeout_seconds
                )

        application = GitHubApplication.__new__(GitHubApplication)
        application.http = Http(self.state)
        application._publish_human_file_request(
            token="synthetic",
            repository="owner/repo",
            repository_id=1,
            pull_request=2,
            review_id=10,
            result=result,
        )
        bodies = [comment["body"] for comment in self.state.comments.values()]
        self.assertEqual(len(bodies), 1)
        self.assertIn(path, bodies[0])
        self.assertIn("I reviewed the media files and I approve", bodies[0])
        self.assertIn("generated files excluded by policy", bodies[0])
