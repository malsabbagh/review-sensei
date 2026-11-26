"""Companion visible-prose overflow publication and reader verification tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse, urlsplit

from default_human_inventory_contract import (
    APP,
    BASE,
    CONFIGURATION,
    EVIDENCE,
    HEAD,
    IDENTITY,
    POLICY,
    DefaultHumanState,
    application,
    located_files,
)
from fake_github_http import json_response
from review_sensei_github import (
    GitHubPublicationError,
    GitHubWriteOptions,
    ReviewPublisher,
)
from review_sensei_github.default_human_inventory import (
    HumanInventoryStore,
    build_human_inventory_context,
)
from review_sensei_github.default_human_reader import DefaultHumanInventoryReader
from review_sensei_github.prose_publication import CommentReviewProseStore
from review_sensei_github.publication import (
    MAX_PUBLISHED_REVIEW_BODY_BYTES,
    PublicationLimitError,
    _disposition_frame_reserve,
    _framed_review_body,
    approval_eligibility_from_body,
    approval_eligibility_marker,
    fit_assembled_review,
    parse_finding_index,
    render_check_warning,
    review_marker,
    review_result_digest,
)
from review_sensei_github.publication_parts import prepare_body_explanation_prose_parts
from review_sensei_github.publication_recovery import publication_http
from review_sensei_github.session_ledger import GitHubIssueCommentSessionLedger
from test_evidence_capacity import human_result, varied_comments
from test_quarantine_evidence import original_objects
from test_quarantine_journey import JourneyFixture

from review_sensei.baseline import ReviewBaseline, baseline_finding_from_comment
from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.context import ReviewContextCacheKey, finding_lifecycle_for_comment
from review_sensei.convergence import derive_blocker_candidate
from review_sensei.errors import ReviewInputError
from review_sensei.human_assessment import finding_instance_fingerprint
from review_sensei.models import ReviewComment, ReviewResult, ReviewTransaction
from review_sensei.presentation import (
    assign_finding_identifiers,
    build_finding_view,
    render_finding,
)
from review_sensei.session import (
    LocalSessionLedger,
    checkpoint_review_analysis,
    prepare_review_transaction,
)


class _VisibleProseOverflowState(DefaultHumanState):
    """Fake GitHub state supporting companion COMMENT, REQUEST_CHANGES, and commands."""

    def __init__(self, comments):
        super().__init__(comments)
        self.app_user["login"] = APP
        self.check_runs = []
        self.files = located_files(comments)

    def open(self, request, timeout):
        parsed = urlparse(request.full_url)
        path = parsed.path
        body = json.loads(request.data) if request.data else None
        if (
            request.method == "GET"
            and "/collaborators/" in path
            and path.endswith("/permission")
        ):
            self.calls.append((request.method, path, body))
            return json_response({"permission": "admin", "role_name": "admin"})
        if request.method == "POST" and path.endswith("/pulls/1/reviews"):
            self.calls.append((request.method, path, body))
            event = body.get("event", "COMMENT")
            state = (
                "APPROVED"
                if event == "APPROVE"
                else "CHANGES_REQUESTED"
                if event == "REQUEST_CHANGES"
                else "COMMENTED"
            )
            review = {
                "id": 40 + len(self.reviews),
                "commit_id": body["commit_id"],
                "state": state,
                "body": body["body"],
                "user": dict(self.app_user),
                "pull_request_url": "https://api.github.test/repos/owner/repo/pulls/1",
            }
            self.reviews.append(review)
            for item in body.get("comments", []):
                self.inline.append(
                    {
                        **item,
                        "id": 2000 + len(self.inline),
                        "pull_request_review_id": review["id"],
                        "commit_id": body["commit_id"],
                        "original_commit_id": body["commit_id"],
                        "user": dict(self.app_user),
                        "pull_request_url": review["pull_request_url"],
                    }
                )
            return json_response(
                review,
                200
                if "comments" in body or event in {"APPROVE", "REQUEST_CHANGES"}
                else 201,
            )
        if request.method == "PUT" and "/pulls/1/reviews/" in path:
            identifier = int(path.rsplit("/", 1)[1])
            self.calls.append((request.method, path, body))
            for review in self.reviews:
                if review["id"] == identifier:
                    review["body"] = body["body"]
                    return json_response(review)
            return json_response({"message": "Not Found"}, 404)
        return super().open(request, timeout)


def _body_placed_result(
    ledger: LocalSessionLedger,
    *,
    count: int = 32,
    body_padding: int = 2300,
    blocker_count: int = 2,
    human_count: int = 4,
    summary: str | None = None,
):
    comments = []
    for index, comment in enumerate(varied_comments(count, detail=3, unique_paths=32)):
        path = f"docs/operations_{index:02d}.md"
        is_blocker = index < blocker_count
        is_human = not is_blocker and index < blocker_count + human_count
        comments.append(
            replace(
                comment,
                path=path,
                line=None,
                side="FILE",
                blocking=is_blocker,
                needs_human=is_human,
                severity="high" if is_blocker or is_human else "medium",
                body=(
                    f"Batch {(index // 4) + 1} finding {index + 1}: "
                    "validate input before transmit() in handler. "
                    + ("detailed remediation guidance " * (body_padding // 29))
                ),
            )
        )
    result = replace(
        human_result(tuple(comments)),
        summary=summary
        or (
            "9-batch review summary across docs/operations_*.md and "
            "resources/docs/guides.json with complete body-placed explanations."
        ),
    )
    prepared = prepare_review_transaction(
        ledger,
        IDENTITY,
        POLICY,
        reservation_id="c" * 64,
        base_sha=BASE,
        head_sha=HEAD,
        configuration_digest=ReviewTransaction.compute_configuration_digest(
            CONFIGURATION
        ),
        evidence_digest=ReviewTransaction.compute_evidence_digest(EVIDENCE),
    )
    key = ReviewContextCacheKey(
        repository=IDENTITY.repository,
        pull_request=IDENTITY.pull_request,
        base_sha=BASE,
        head_sha=HEAD,
        engine=result.provider,
        model=CONFIGURATION["model"],
        profile="default",
        stage_digest="1" * 64,
        context_digest="2" * 64,
        learning_digest="3" * 64,
    )
    baseline = ReviewBaseline(
        cache_key=key,
        policy_digest=POLICY.digest(),
        complete=True,
        coverage_complete=True,
        generation=prepared.record.generation + 1,
        findings=tuple(
            sorted(
                (baseline_finding_from_comment(c) for c in result.comments),
                key=lambda finding: finding.fingerprint,
            )
        ),
        reviewed_paths=tuple(sorted({c.path for c in result.comments})),
    )
    return (
        checkpoint_review_analysis(
            ledger, IDENTITY, prepared, result, baseline=baseline
        ),
        key,
    )


def _blocker_candidates(comments):
    return tuple(
        derive_blocker_candidate(
            comment,
            on_changed_path=True,
            evidence_locations_validated=comment.blocks_approval,
            has_failure_condition=comment.blocks_approval,
            has_specific_violation=comment.blocks_approval,
            has_actionable_remedy=comment.blocks_approval,
        )
        for comment in comments
    )


def _expected_body_explanations(result, reviews):
    main_review = next(
        r for r in reviews if "<!-- reviewsensei:finding-index:" in r["body"]
    )
    index = parse_finding_index(
        main_review["body"],
        repository_id=IDENTITY.repository_id,
        pull_request=IDENTITY.pull_request,
        head_sha=HEAD,
    )
    assert index is not None
    entries = {entry.instance: entry for entry in index.entries}
    instances = [finding_instance_fingerprint(c) for c in result.comments]
    identifiers = assign_finding_identifiers(instances)
    rendered = {}
    for comment, instance in zip(result.comments, instances, strict=True):
        entry = entries[instance]
        if not entry.body_placed:
            continue
        rendered[instance] = render_finding(
            replace(
                build_finding_view(
                    comment,
                    fingerprint=finding_lifecycle_for_comment(comment).fingerprint,
                    identifier=identifiers[instance],
                    placement="body",
                ),
                repeat=True,
            )
        )
    return main_review, index, rendered


class _DriftOverflowJourneyFixture(JourneyFixture):
    """Hosted journey fixture supporting per-review inline comment readback."""

    def external(self, request, timeout):
        path = urlsplit(request.full_url).path
        if (
            request.get_method() == "GET"
            and path.startswith("/repos/owner/repo/pulls/7/reviews/")
            and path.endswith("/comments")
        ):
            self._meter(request)
            return json_response([])
        return super().external(request, timeout)


class VisibleProseOverflowTests(unittest.TestCase):
    def test_30_plus_body_findings_overflow_with_and_without_drift_projection(self):
        with self.subTest(drift=False), tempfile.TemporaryDirectory() as directory:
            ledger_dir = Path(directory) / "ledger"
            init_ledger = LocalSessionLedger(ledger_dir)
            result, key = _body_placed_result(
                init_ledger, count=32, body_padding=2300, blocker_count=2, human_count=4
            )
            total_prose_bytes = sum(
                len(c.body.encode("utf-8")) for c in result.comments
            )
            self.assertGreater(total_prose_bytes, 70_000)

            state = _VisibleProseOverflowState(result.comments)
            ledger = LocalSessionLedger(
                ledger_dir, enable_finding_lifecycle_writes=True
            )
            candidates = _blocker_candidates(result.comments)
            options = GitHubWriteOptions(
                github_writes=True,
                auto_review=True,
                auto_approve=True,
                complete_inventory=True,
                enable_drift_tracking=False,
                allow_visible_prose_overflow=True,
            )
            publication = application(state, ledger).publish_review(
                options=options,
                oidc_token="synthetic",
                repository=IDENTITY.repository,
                repository_id=IDENTITY.repository_id,
                pull_request=IDENTITY.pull_request,
                head_sha=HEAD,
                base_sha=BASE,
                base_branch="main",
                result=result,
                diff=state.diff,
                app_slug=APP,
                convergence_policy=POLICY,
                current_key=key,
                configuration_context=CONFIGURATION,
                evidence_context=EVIDENCE,
                blocker_candidates=candidates,
            )
            self.assertEqual(publication.status, "published")
            for rev in state.reviews:
                self.assertLessEqual(
                    len(rev["body"].encode("utf-8")), MAX_PUBLISHED_REVIEW_BODY_BYTES
                )

            main_review, _index, expected_bodies = _expected_body_explanations(
                result, state.reviews
            )
            companion_reviews = [
                r
                for r in state.reviews
                if r["id"] != main_review["id"]
                and r["state"] == "COMMENTED"
                and "## ReviewSensei finding explanations — part " in r["body"]
            ]
            self.assertGreaterEqual(len(companion_reviews), 2)
            for comp in companion_reviews:
                self.assertNotIn("<!-- reviewsensei:", comp["body"])
                self.assertIn(f"#pullrequestreview-{comp['id']}", main_review["body"])

            combined_companion_body = "\n".join(c["body"] for c in companion_reviews)
            for _inst, explanation in expected_bodies.items():
                self.assertNotIn(explanation, main_review["body"])
                self.assertIn(explanation, combined_companion_body)

            # Cold DefaultHumanInventoryReader.validate_root must succeed.
            eligibility = application(
                state, LocalSessionLedger(ledger_dir)
            ).reviewer.finalizer.load_eligibility(
                token="synthetic-read",
                repository=IDENTITY.repository,
                pull_request=IDENTITY.pull_request,
                head_sha=HEAD,
                app_slug=APP,
                require_valid=True,
                validate_visible_prose=True,
            )
            self.assertIsNotNone(eligibility)
            self.assertEqual(len(eligibility.human_review.pending), 4)

            # Idempotent replay produces already_published and 0 duplicate companion reviews.
            review_count_before_replay = len(state.reviews)
            replay = application(state, LocalSessionLedger(ledger_dir)).publish_review(
                options=options,
                oidc_token="synthetic",
                repository=IDENTITY.repository,
                repository_id=IDENTITY.repository_id,
                pull_request=IDENTITY.pull_request,
                head_sha=HEAD,
                base_sha=BASE,
                base_branch="main",
                result=result,
                diff=state.diff,
                app_slug=APP,
                convergence_policy=POLICY,
                current_key=key,
                configuration_context=CONFIGURATION,
                evidence_context=EVIDENCE,
                blocker_candidates=candidates,
            )
            self.assertEqual(replay.status, "already_published")
            self.assertEqual(len(state.reviews), review_count_before_replay)

            # Each webhook/comment command runs with its own 64-call budget.
            cmd_options = GitHubWriteOptions(
                github_writes=True,
                auto_review=True,
                auto_approve=True,
                complete_inventory=True,
                enable_drift_tracking=False,
                allow_visible_prose_overflow=True,
            )
            for comment in result.comments[2:6]:
                cmd = application(
                    state,
                    LocalSessionLedger(
                        ledger_dir, enable_finding_lifecycle_writes=True
                    ),
                ).apply_maintainer_command(
                    options=cmd_options,
                    oidc_token="synthetic-oidc",
                    repository=IDENTITY.repository,
                    repository_id=IDENTITY.repository_id,
                    pull_request=IDENTITY.pull_request,
                    head_sha=HEAD,
                    body=(
                        "@reviewsensei dismiss "
                        f"{finding_instance_fingerprint(comment)} "
                        '--reason "Verified acceptable."'
                    ),
                    actor_login="maintainer",
                    association="OWNER",
                    app_slug=APP,
                )
                self.assertTrue(cmd.applied, cmd)
            for comment in result.comments[:2]:
                cmd = application(
                    state,
                    LocalSessionLedger(
                        ledger_dir, enable_finding_lifecycle_writes=True
                    ),
                ).apply_maintainer_command(
                    options=cmd_options,
                    oidc_token="synthetic-oidc",
                    repository=IDENTITY.repository,
                    repository_id=IDENTITY.repository_id,
                    pull_request=IDENTITY.pull_request,
                    head_sha=HEAD,
                    body=(
                        "@reviewsensei override "
                        f"{finding_instance_fingerprint(comment)}"
                    ),
                    actor_login="maintainer",
                    association="OWNER",
                    app_slug=APP,
                )
                self.assertTrue(cmd.applied, cmd)
            self.assertEqual(state.events().count("APPROVE"), 1)

        with self.subTest(drift=True), tempfile.TemporaryDirectory() as directory:
            # Real hosted review_run entrypoint enables drift projection and companion offload.
            f = _DriftOverflowJourneyFixture(directory)
            original_objects(f)
            f.provider_summary = "9-batch review summary.\n" + ("Overview line.\n" * 40)
            f.provider_comments = [
                {
                    "path": "src/app.py",
                    "side": "FILE",
                    "body": (
                        f"Batch {(i // 4) + 1} finding {i + 1}: "
                        "validate input before transmit() in handler. "
                        + ("detailed remediation guidance " * 80)
                    ),
                    "severity": "high" if i < 6 else "low",
                    "blocking": i < 2,
                    "needs_human": 2 <= i < 6,
                }
                for i in range(32)
            ]
            total_bytes = sum(
                len(c["body"].encode("utf-8")) for c in f.provider_comments
            )
            self.assertGreater(total_bytes, 70_000)
            self.assertEqual(f.invoke(), 0, f.error)
            for rev in f.reviews:
                self.assertLessEqual(
                    len(rev["body"].encode("utf-8")), MAX_PUBLISHED_REVIEW_BODY_BYTES
                )
            companion_reviews = [
                r
                for r in f.reviews
                if r["state"] == "COMMENTED"
                and "## ReviewSensei finding explanations — part " in r["body"]
            ]
            self.assertGreaterEqual(len(companion_reviews), 2)
            main_review = f.reviews[-1]
            for comp in companion_reviews:
                self.assertNotIn("<!-- reviewsensei:", comp["body"])
                self.assertIn(f"#pullrequestreview-{comp['id']}", main_review["body"])

            # Cold DefaultHumanInventoryReader.validate_root passes on hosted drift root.
            pub = ReviewPublisher(http=f.http)
            budget = EvidenceReadBudget()
            pub.finalizer.evidence_budget = budget
            app_slug = "reviewsensei[bot]"
            reader = DefaultHumanInventoryReader(
                http=f.http,
                ledger=GitHubIssueCommentSessionLedger(
                    f.http, token="synthetic", app_slug=app_slug, evidence_budget=budget
                ),
                token="synthetic",
                repository="owner/repo",
                pull_request=7,
                head_sha=f.live_head,
                app_slug=app_slug,
                budget=budget,
            )
            pub.finalizer.human_inventory_reader_factory = lambda **_: reader
            elig = pub.finalizer.load_eligibility(
                token="synthetic",
                repository="owner/repo",
                pull_request=7,
                head_sha=f.live_head,
                app_slug=app_slug,
                require_valid=True,
                validate_visible_prose=True,
            )
            self.assertIsNotNone(elig)
            self.assertEqual(len(elig.human_review.pending), 6)

            # Idempotent replay on the same head produces 0 duplicate reviews and 0 provider calls.
            review_count_before = len(f.reviews)
            provider_calls_before = f.provider_calls
            saved_head = f.live_head
            f.next_run()
            f.live_head = saved_head
            f.requested_head = saved_head
            self.assertEqual(f.invoke(), 0, f.error)
            self.assertEqual(f.provider_calls, provider_calls_before)
            self.assertEqual(len(f.reviews), review_count_before)

    def test_fail_closed_with_zero_mutations_when_directory_or_parts_exceed_limits(
        self,
    ):
        # Case A: directory + markers + reserves exceed 65,536 bytes -> fail closed, 0 mutations.
        with tempfile.TemporaryDirectory() as directory:
            ledger_dir = Path(directory) / "oversized_dir"
            init_ledger = LocalSessionLedger(ledger_dir)
            comments = tuple(
                ReviewComment(
                    path=f"docs/op_{idx:03d}.md",
                    line=None,
                    side="FILE",
                    body=f"Finding {idx}: ensure bounded handling. " + ("detail " * 80),
                    severity="high",
                    blocking=True,
                    needs_human=True,
                )
                for idx in range(250)
            )
            result = ReviewResult(
                summary="Overview narrative line.\n" * 1300,
                comments=comments,
                provider="synthetic",
                review_status="complete",
            )
            prepared = prepare_review_transaction(
                init_ledger,
                IDENTITY,
                POLICY,
                reservation_id="c" * 64,
                base_sha=BASE,
                head_sha=HEAD,
                configuration_digest=ReviewTransaction.compute_configuration_digest(
                    CONFIGURATION
                ),
                evidence_digest=ReviewTransaction.compute_evidence_digest(EVIDENCE),
            )
            result = checkpoint_review_analysis(init_ledger, IDENTITY, prepared, result)
            state = _VisibleProseOverflowState(result.comments)
            budget = EvidenceReadBudget()
            http = publication_http(state.http, budget)
            publisher = ReviewPublisher(http=http)
            publisher.finalizer.evidence_budget = budget
            ctx = build_human_inventory_context(
                http=http,
                token="synthetic",
                ledger=LocalSessionLedger(ledger_dir),
                identity=IDENTITY,
                base_sha=BASE,
                head_sha=HEAD,
                app_slug=APP,
                publication_digest=review_result_digest(result),
                policy_digest=POLICY.digest(),
                configuration_digest=ReviewTransaction.compute_configuration_digest(
                    CONFIGURATION
                ),
                evidence_digest=ReviewTransaction.compute_evidence_digest(EVIDENCE),
                budget=budget,
            )
            inv_store = HumanInventoryStore(
                http, "synthetic", LocalSessionLedger(ledger_dir), ctx
            )
            prose_store = CommentReviewProseStore(
                http=state.http,
                token="synthetic",
                repository=IDENTITY.repository,
                pull_request=IDENTITY.pull_request,
                producer_id=state.app_user["id"],
                app_slug=APP,
                budget=budget,
            )
            with self.assertRaises(PublicationLimitError) as caught:
                publisher.publish(
                    token="synthetic",
                    check_token=None,
                    repository=IDENTITY.repository,
                    repository_id=IDENTITY.repository_id,
                    pull_request=IDENTITY.pull_request,
                    head_sha=HEAD,
                    base_sha=BASE,
                    base_branch="main",
                    result=result,
                    diff=state.diff,
                    app_slug=APP,
                    convergence_policy=POLICY,
                    complete_inventory=True,
                    human_inventory_store=inv_store,
                    allow_human_inventory_start=True,
                    visible_prose_store=prose_store,
                )
            self.assertEqual(caught.exception.check, "framed-review-body")
            self.assertIsNone(inv_store.authenticated_reference)
            self.assertEqual(state.reviews, [])
            self.assertEqual(state.check_runs, [])
            self.assertEqual(state.parts, [])

        # Case B: aggregate prose > 1 MiB or > 32 parts -> fail closed, 0 mutations.
        with tempfile.TemporaryDirectory() as directory:
            ledger_dir = Path(directory) / "over_1mib"
            init_ledger = LocalSessionLedger(ledger_dir)
            comments = tuple(
                ReviewComment(
                    path=f"docs/large_{idx:02d}.md",
                    line=None,
                    side="FILE",
                    body=f"Finding {idx}: ensure bounded handling. "
                    + ("detail " * 2000),
                    severity="high",
                    blocking=False,
                    needs_human=False,
                )
                for idx in range(80)
            )
            result = ReviewResult(
                summary="Overview.",
                comments=comments,
                provider="synthetic",
                review_status="complete",
            )
            prepared = prepare_review_transaction(
                init_ledger,
                IDENTITY,
                POLICY,
                reservation_id="d" * 64,
                base_sha=BASE,
                head_sha=HEAD,
                configuration_digest=ReviewTransaction.compute_configuration_digest(
                    CONFIGURATION
                ),
                evidence_digest=ReviewTransaction.compute_evidence_digest(EVIDENCE),
            )
            result = checkpoint_review_analysis(init_ledger, IDENTITY, prepared, result)
            state = _VisibleProseOverflowState(result.comments)
            budget = EvidenceReadBudget()
            http = publication_http(state.http, budget)
            publisher = ReviewPublisher(http=http)
            publisher.finalizer.evidence_budget = budget
            ctx = build_human_inventory_context(
                http=http,
                token="synthetic",
                ledger=LocalSessionLedger(ledger_dir),
                identity=IDENTITY,
                base_sha=BASE,
                head_sha=HEAD,
                app_slug=APP,
                publication_digest=review_result_digest(result),
                policy_digest=POLICY.digest(),
                configuration_digest=ReviewTransaction.compute_configuration_digest(
                    CONFIGURATION
                ),
                evidence_digest=ReviewTransaction.compute_evidence_digest(EVIDENCE),
                budget=budget,
            )
            inv_store = HumanInventoryStore(
                http, "synthetic", LocalSessionLedger(ledger_dir), ctx
            )
            prose_store = CommentReviewProseStore(
                http=state.http,
                token="synthetic",
                repository=IDENTITY.repository,
                pull_request=IDENTITY.pull_request,
                producer_id=state.app_user["id"],
                app_slug=APP,
                budget=budget,
            )
            with self.assertRaises(ReviewInputError):
                publisher.publish(
                    token="synthetic",
                    check_token=None,
                    repository=IDENTITY.repository,
                    repository_id=IDENTITY.repository_id,
                    pull_request=IDENTITY.pull_request,
                    head_sha=HEAD,
                    base_sha=BASE,
                    base_branch="main",
                    result=result,
                    diff=state.diff,
                    app_slug=APP,
                    convergence_policy=POLICY,
                    complete_inventory=True,
                    human_inventory_store=inv_store,
                    allow_human_inventory_start=True,
                    visible_prose_store=prose_store,
                )
            self.assertIsNone(inv_store.authenticated_reference)
            self.assertEqual(state.reviews, [])
            self.assertEqual(state.check_runs, [])
            self.assertEqual(state.parts, [])

            # Also verify prepare_body_explanation_prose_parts fails on > 32 parts / > 1 MiB.
            from review_sensei_github.publication import BodyExplanation

            oversized_explanations = tuple(
                BodyExplanation(
                    instance=f"{idx:064x}",
                    severity="high",
                    obligatory=False,
                    view=build_finding_view(
                        ReviewComment(
                            path=f"docs/part_{idx:02d}.md",
                            line=None,
                            side="FILE",
                            body=f"Part {idx}: " + ("x" * 16_000),
                            severity="high",
                        ),
                        fingerprint=f"{idx:064x}",
                        identifier=f"RS-{idx:06X}",
                        placement="body",
                    ),
                )
                for idx in range(70)
            )
            with self.assertRaises(ReviewInputError):
                prepare_body_explanation_prose_parts(
                    oversized_explanations, head_sha=HEAD
                )

    def test_tampered_foreign_and_missing_companion_review_fails_closed(self):
        for mode in ("tampered", "foreign", "missing"):
            with (
                self.subTest(mode=mode),
                tempfile.TemporaryDirectory() as directory,
            ):
                ledger_dir = Path(directory) / "ledger"
                init_ledger = LocalSessionLedger(ledger_dir)
                result, key = _body_placed_result(
                    init_ledger,
                    count=32,
                    body_padding=2300,
                    blocker_count=1,
                    human_count=2,
                )
                state = _VisibleProseOverflowState(result.comments)
                options = GitHubWriteOptions(
                    github_writes=True,
                    auto_review=True,
                    complete_inventory=True,
                    allow_visible_prose_overflow=True,
                )
                pub = application(state, LocalSessionLedger(ledger_dir)).publish_review(
                    options=options,
                    oidc_token="synthetic",
                    repository=IDENTITY.repository,
                    repository_id=IDENTITY.repository_id,
                    pull_request=IDENTITY.pull_request,
                    head_sha=HEAD,
                    base_sha=BASE,
                    base_branch="main",
                    result=result,
                    diff=state.diff,
                    app_slug=APP,
                    convergence_policy=POLICY,
                    current_key=key,
                    configuration_context=CONFIGURATION,
                    evidence_context=EVIDENCE,
                    blocker_candidates=_blocker_candidates(result.comments),
                )
                self.assertEqual(pub.status, "published")
                main_review, _index, _bodies = _expected_body_explanations(
                    result, state.reviews
                )
                comp_idx = next(
                    i
                    for i, r in enumerate(state.reviews)
                    if r["id"] != main_review["id"]
                    and "## ReviewSensei finding explanations — part " in r["body"]
                )
                if mode == "tampered":
                    state.reviews[comp_idx]["body"] += " tampered"
                elif mode == "foreign":
                    state.reviews[comp_idx]["user"] = {
                        "id": 999_999,
                        "login": "foreign[bot]",
                        "type": "Bot",
                    }
                else:
                    state.reviews.pop(comp_idx)

                with self.assertRaises((GitHubPublicationError, ReviewInputError)):
                    application(
                        state, LocalSessionLedger(ledger_dir)
                    ).reviewer.finalizer.load_eligibility(
                        token="synthetic-read",
                        repository=IDENTITY.repository,
                        pull_request=IDENTITY.pull_request,
                        head_sha=HEAD,
                        app_slug=APP,
                        require_valid=True,
                        validate_visible_prose=True,
                    )

                with self.assertRaises((GitHubPublicationError, ReviewInputError)):
                    application(state, LocalSessionLedger(ledger_dir)).publish_review(
                        options=options,
                        oidc_token="synthetic",
                        repository=IDENTITY.repository,
                        repository_id=IDENTITY.repository_id,
                        pull_request=IDENTITY.pull_request,
                        head_sha=HEAD,
                        base_sha=BASE,
                        base_branch="main",
                        result=result,
                        diff=state.diff,
                        app_slug=APP,
                        convergence_policy=POLICY,
                        current_key=key,
                        configuration_context=CONFIGURATION,
                        evidence_context=EVIDENCE,
                        blocker_candidates=_blocker_candidates(result.comments),
                    )
                self.assertNotIn("APPROVED", state.events())

    def test_preflight_reserve_matches_final_emit_with_restart_marker_and_check_warning(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            ledger_dir = Path(directory) / "ledger"
            init_ledger = LocalSessionLedger(ledger_dir, enable_partition_writes=True)
            padded_comments = tuple(
                ReviewComment(
                    path=f"docs/operations_{idx:02d}.md",
                    line=None,
                    side="FILE",
                    body=(
                        f"Finding {idx}: validate input before transmit(). "
                        + ("x" * (13_000 if idx < 4 else 0))
                    ),
                    severity="high",
                    blocking=True,
                    needs_human=False,
                )
                for idx in range(5)
            )
            base_result = replace(
                human_result(padded_comments),
                summary="Short overview.",
            )
            state = _VisibleProseOverflowState(base_result.comments)
            budget = EvidenceReadBudget()
            http = publication_http(state.http, budget)
            restart_marker_text = f"<!-- reviewsensei:restart:v1 id={'a' * 64} -->"
            state.http._controlled_restart_guard = SimpleNamespace(
                admission=SimpleNamespace(marker=restart_marker_text),
                owned=set(),
                before_request=lambda *args: None,
                after_response=lambda *args: None,
            )
            publisher = ReviewPublisher(http=http)
            publisher.finalizer.evidence_budget = budget

            # Measure exact room left after markers, disposition reserve, restart marker, and check warning.
            eligibility = approval_eligibility_from_body(
                approval_eligibility_marker(
                    publisher._eligibility_with_carried(
                        base_result,
                        head_sha=HEAD,
                        base_sha=BASE,
                        enabled=True,
                        app_authored=False,
                        qualification="not-required",
                    )
                )
            )
            assert eligibility is not None
            probe_assembled = publisher._assemble_review_body(
                result=base_result,
                analysis=publisher._validate_locations(base_result, state.diff),
                convergence_policy=POLICY,
                read_facts=lambda: publisher._conversation_resolution_facts(
                    token="synthetic", repository=IDENTITY.repository, branch="main"
                ),
                auto_approve=False,
                repository_id=IDENTITY.repository_id,
                pull_request=IDENTITY.pull_request,
                head_sha=HEAD,
                base_sha=BASE,
            )
            probe_marker = review_marker(
                repository_id=IDENTITY.repository_id,
                pull_request=IDENTITY.pull_request,
                head_sha=HEAD,
                result=base_result,
            )
            probe_elig_marker = approval_eligibility_marker(eligibility)
            disp_reserve = _disposition_frame_reserve(
                eligibility, probe_assembled.index_entries
            )
            warning_bytes = (
                len(
                    render_check_warning(
                        "check_capability_unavailable", concise=True
                    ).encode("utf-8")
                )
                + 2
            )
            restart_bytes = len(("\n" + restart_marker_text).encode("utf-8"))
            summary_now, index_now = fit_assembled_review(
                probe_assembled,
                review_marker=probe_marker,
                eligibility_marker=probe_elig_marker,
                repository_id=IDENTITY.repository_id,
                pull_request=IDENTITY.pull_request,
                head_sha=HEAD,
                base_sha=BASE,
                result_digest=review_result_digest(base_result),
                summary_limit=65_536,
                complete_inventory=True,
                reserved_bytes=disp_reserve + warning_bytes + restart_bytes,
                preserve_explanations=True,
            )
            framed_now = _framed_review_body(
                summary_now,
                review_marker=probe_marker,
                index_marker=index_now,
                eligibility_marker=probe_elig_marker,
            )
            remaining = (
                MAX_PUBLISHED_REVIEW_BODY_BYTES
                - len(framed_now.encode("utf-8"))
                - disp_reserve
                - warning_bytes
                - restart_bytes
            )

            # Pad the final comment by `remaining + 10` bytes -> fits without warning/restart reserves,
            # MUST fail in preflight before any mutations when check_token=None + restart_marker!
            over_comment = replace(
                padded_comments[-1],
                body=padded_comments[-1].body + " " + ("z" * (remaining + 10)),
            )
            over_result = replace(
                base_result,
                comments=(*padded_comments[:-1], over_comment),
                summary="Short overview.",
            )
            prepared_over = prepare_review_transaction(
                init_ledger,
                IDENTITY,
                POLICY,
                reservation_id="e" * 64,
                base_sha=BASE,
                head_sha=HEAD,
                configuration_digest=ReviewTransaction.compute_configuration_digest(
                    CONFIGURATION
                ),
                evidence_digest=ReviewTransaction.compute_evidence_digest(EVIDENCE),
            )
            over_result = checkpoint_review_analysis(
                init_ledger, IDENTITY, prepared_over, over_result
            )
            over_ctx = build_human_inventory_context(
                http=http,
                token="synthetic",
                ledger=LocalSessionLedger(ledger_dir),
                identity=IDENTITY,
                base_sha=BASE,
                head_sha=HEAD,
                app_slug=APP,
                publication_digest=review_result_digest(over_result),
                policy_digest=POLICY.digest(),
                configuration_digest=ReviewTransaction.compute_configuration_digest(
                    CONFIGURATION
                ),
                evidence_digest=ReviewTransaction.compute_evidence_digest(EVIDENCE),
                budget=budget,
            )
            over_store = HumanInventoryStore(
                http, "synthetic", LocalSessionLedger(ledger_dir), over_ctx
            )
            with self.assertRaises(PublicationLimitError) as caught:
                publisher.publish(
                    token="synthetic",
                    check_token=None,
                    repository=IDENTITY.repository,
                    repository_id=IDENTITY.repository_id,
                    pull_request=IDENTITY.pull_request,
                    head_sha=HEAD,
                    base_sha=BASE,
                    base_branch="main",
                    result=over_result,
                    diff=state.diff,
                    app_slug=APP,
                    convergence_policy=POLICY,
                    complete_inventory=True,
                    human_inventory_store=over_store,
                    allow_human_inventory_start=True,
                )
            self.assertEqual(caught.exception.check, "framed-review-body")
            # Preflight refused BEFORE staging human inventory or posting reviews!
            self.assertIsNone(over_store.authenticated_reference)
            self.assertEqual(state.reviews, [])
            self.assertEqual(state.parts, [])

            # Now enable visible_prose_store -> offloads cleanly and final emit stays <= 65,536!
            prose_store = CommentReviewProseStore(
                http=state.http,
                token="synthetic",
                repository=IDENTITY.repository,
                pull_request=IDENTITY.pull_request,
                producer_id=state.app_user["id"],
                app_slug=APP,
                budget=budget,
            )
            pub = publisher.publish(
                token="synthetic",
                check_token=None,
                repository=IDENTITY.repository,
                repository_id=IDENTITY.repository_id,
                pull_request=IDENTITY.pull_request,
                head_sha=HEAD,
                base_sha=BASE,
                base_branch="main",
                result=over_result,
                diff=state.diff,
                app_slug=APP,
                convergence_policy=POLICY,
                complete_inventory=True,
                human_inventory_store=over_store,
                allow_human_inventory_start=True,
                visible_prose_store=prose_store,
            )
            self.assertEqual(pub.status, "published")
            for rev in state.reviews:
                self.assertLessEqual(
                    len(rev["body"].encode("utf-8")), MAX_PUBLISHED_REVIEW_BODY_BYTES
                )
            self.assertTrue(
                any(restart_marker_text in rev["body"] for rev in state.reviews)
            )
            self.assertTrue(
                any(
                    "Warning: ReviewSensei merge gate unconfirmed" in rev["body"]
                    for rev in state.reviews
                )
            )
